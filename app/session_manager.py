"""
BOMA v2.1 — Layer 4: Session Manager (Enhanced)
=================================================
Manages conversation context WITHOUT stale entity bleed.

ENHANCEMENTS vs v2.0:
  - Tracks ALL materials mentioned across conversation
  - Tracks materials_processed to support "what about others"
  - Tracks materials_pending for follow-up resolution
  - Budget persists across turns (once mentioned, remembered)
  - Last objective remembered for follow-up routing
  - Conversation history summary (last 3 turns) for context

Rules:
  1. Current query entities ALWAYS override session
  2. Session stores ONLY reusable BUSINESS CONTEXT
  3. Session has TTL — old threads are auto-expired
  4. Intermediate calculation results kept in exec_state (cleared per request)
"""

from __future__ import annotations

import time
import logging
from typing import Optional

logger = logging.getLogger(__name__)

THREAD_TTL_SECONDS = 3600   # sessions expire after 1 hour of inactivity
MAX_HISTORY_TURNS  = 5      # keep last 5 Q&A pairs for context


class SessionManager:
    def __init__(self):
        self._sessions: dict[str, dict] = {}
        self._last_active: dict[str, float] = {}

    def get(self, thread_id: str) -> dict:
        """Get session — create empty one if not exists."""
        self._evict_expired()
        if thread_id not in self._sessions:
            self._sessions[thread_id] = {
                "material":             None,
                "materials":            [],          # ALL materials ever mentioned
                "materials_mentioned":  [],          # ordered list in conversation
                "materials_processed":  [],          # materials we already answered
                "materials_pending":    [],          # materials not yet answered
                "plants":               [],
                "budget":               None,
                "budget_currency":      "USD",
                "last_objective":       None,
                "bom_number":           None,
                "conversation_history": [],          # [{q, a, objective}]
                "exec_state":           {},
            }
        self._touch(thread_id)
        return self._sessions[thread_id]

    def update_from_entities(self, thread_id: str, entities: dict) -> None:
        """
        Update session with entities extracted from CURRENT query.
        Only updates fields that were actually found in the current query.
        Never overwrites with None.
        """
        session = self.get(thread_id)
        found = entities.get("entities_found_in_query", [])

        # ── Material ──────────────────────────────────────────────────────────
        if "material" in found and entities.get("material"):
            old = session.get("material")
            session["material"] = entities["material"]
            if old and old != entities["material"]:
                logger.info("[session] material changed: %s → %s", old, entities["material"])

        # ── Multiple materials ────────────────────────────────────────────────
        new_materials = entities.get("materials", [])
        if new_materials:
            # Merge into master list (preserve order, deduplicate)
            existing = session.get("materials_mentioned", [])
            for m in new_materials:
                if m not in existing:
                    existing.append(m)
            session["materials_mentioned"] = existing

            # Update materials_pending: newly mentioned, not yet processed
            processed = set(session.get("materials_processed", []))
            session["materials_pending"] = [
                m for m in existing if m not in processed
            ]

            # Update ALL materials set
            all_mats = session.get("materials", [])
            for m in new_materials:
                if m not in all_mats:
                    all_mats.append(m)
            session["materials"] = all_mats

        # ── Plants ────────────────────────────────────────────────────────────
        if "plants" in found and entities.get("plants"):
            old = session.get("plants", [])
            session["plants"] = entities["plants"]
            if old and old != entities["plants"]:
                logger.info("[session] plants changed: %s → %s", old, entities["plants"])

        # ── Budget (persists once mentioned) ──────────────────────────────────
        if "budget" in found and entities.get("budget") is not None:
            session["budget"] = entities["budget"]
            session["budget_currency"] = entities.get("budget_currency", "USD")

        # ── BOM number ────────────────────────────────────────────────────────
        if "bom_number" in found and entities.get("bom_number"):
            session["bom_number"] = entities["bom_number"]

        self._touch(thread_id)

    def mark_material_processed(self, thread_id: str, material: str) -> None:
        """Mark a material as having been answered — used for follow-up tracking."""
        session = self.get(thread_id)
        processed = session.get("materials_processed", [])
        if material not in processed:
            processed.append(material)
            session["materials_processed"] = processed
        # Update pending list
        pending = [m for m in session.get("materials_mentioned", []) if m not in processed]
        session["materials_pending"] = pending

    def get_pending_materials(self, thread_id: str) -> list[str]:
        """Get materials mentioned but not yet answered."""
        session = self.get(thread_id)
        return list(session.get("materials_pending", []))

    def get_next_pending(self, thread_id: str, followup_type: str) -> list[str]:
        """
        Resolve which materials to process for a follow-up query.
        Returns list of materials to process.
        """
        session = self.get(thread_id)
        pending  = list(session.get("materials_pending", []))
        all_mats = list(session.get("materials_mentioned", []))

        if followup_type in ("others", "remaining", "rest"):
            return pending  # everything not yet processed
        elif followup_type in ("all_of_them", "same_for_all"):
            return all_mats  # all materials ever mentioned
        elif followup_type == "continue":
            return pending[:1] if pending else []  # next one in queue
        else:
            return pending  # default: return all pending

    def update_objective(self, thread_id: str, objective: str) -> None:
        session = self.get(thread_id)
        session["last_objective"] = objective
        self._touch(thread_id)

    def add_conversation_turn(self, thread_id: str, question: str, answer: str, objective: str) -> None:
        """Add a Q&A turn to conversation history (kept for context injection)."""
        session = self.get(thread_id)
        history = session.get("conversation_history", [])
        history.append({
            "q":         question[:300],
            "a":         answer[:500],
            "objective": objective,
            "ts":        time.time(),
        })
        # Keep last N turns only
        session["conversation_history"] = history[-MAX_HISTORY_TURNS:]
        # Also store the last objective so follow-ups can inherit it
        session["last_objective"] = objective
        self._touch(thread_id)

    def get_conversation_summary(self, thread_id: str) -> str:
        """
        Build a rich conversation context string injected into every agent prompt.
        Helps agent remember budget, materials, plants, objectives, and prior answers.
        """
        session = self.get(thread_id)
        parts: list[str] = []

        # Budget — persists across all turns once mentioned
        if session.get("budget"):
            curr = session.get("budget_currency", "USD")
            parts.append(f"ESTABLISHED BUDGET: {curr} {session['budget']:,.0f}")

        # Current material context
        if session.get("material"):
            parts.append(f"LAST MATERIAL: {session['material']}")

        # Plant context
        if session.get("plants"):
            parts.append(f"LAST PLANTS: {', '.join(session['plants'])}")

        # BOM number
        if session.get("bom_number"):
            parts.append(f"LAST BOM: {session['bom_number']}")

        # Multi-material tracking
        all_mentioned = session.get("materials_mentioned", [])
        processed     = session.get("materials_processed", [])
        pending       = [m for m in all_mentioned if m not in processed]

        if all_mentioned:
            parts.append(f"ALL MATERIALS MENTIONED: {', '.join(all_mentioned)}")
        if processed:
            parts.append(f"ALREADY ANSWERED: {', '.join(processed)}")
        if pending:
            parts.append(f"STILL TO ANSWER: {', '.join(pending)}")

        # Last objective — critical for follow-up routing
        last_obj = session.get("last_objective", "")
        if last_obj:
            parts.append(f"LAST QUERY TYPE: {last_obj}")

        # Recent conversation history (last 2 turns for context)
        history = session.get("conversation_history", [])
        if history:
            recent = history[-2:]
            for turn in recent:
                parts.append(f"PREV Q: {turn['q'][:120]}")
                parts.append(f"PREV A (summary): {turn['a'][:200]}")

        return "\n".join(parts) if parts else ""

    def set_exec_state(self, thread_id: str, key: str, value) -> None:
        """Store intermediate calculation result within a request."""
        session = self.get(thread_id)
        session.setdefault("exec_state", {})[key] = value

    def get_exec_state(self, thread_id: str, key: str, default=None):
        session = self.get(thread_id)
        return session.get("exec_state", {}).get(key, default)

    def clear_exec_state(self, thread_id: str) -> None:
        """Clear per-request intermediate state after response is sent."""
        session = self.get(thread_id)
        session["exec_state"] = {}

    def _touch(self, thread_id: str) -> None:
        self._last_active[thread_id] = time.monotonic()

    def _evict_expired(self) -> None:
        now = time.monotonic()
        expired = [
            tid for tid, ts in list(self._last_active.items())
            if now - ts > THREAD_TTL_SECONDS
        ]
        for tid in expired:
            self._sessions.pop(tid, None)
            self._last_active.pop(tid, None)
        if expired:
            logger.info("[session] evicted %d expired session(s)", len(expired))


# Singleton instance used by the agent
_session_manager = SessionManager()


def get_session_manager() -> SessionManager:
    return _session_manager
