"""
Agent tool unit tests — BOM Cost Optimization Agent.

All tests run in IBD_TESTING=1 mode (set in conftest.py) so MCP calls
return deterministic mock data without network access.
"""
import json
import os
import pytest

# IBD_TESTING is set to "1" by conftest.py before imports
assert os.environ.get("IBD_TESTING") == "1", "IBD_TESTING must be set"


class TestClassifyMakeBuy:
    """Unit tests for the Make/Buy/Strategic Buy classification logic."""

    def test_make_inhouse_production(self, add_agent_to_path):
        from tools import classify_make_buy
        result = classify_make_buy("MAT-001", cost_variance_percent=25.0, procurement_type="E")
        assert result["recommendation"] == "Make"
        assert result["materialNumber"] == "MAT-001"

    def test_make_low_variance(self, add_agent_to_path):
        from tools import classify_make_buy
        result = classify_make_buy("MAT-002", cost_variance_percent=3.0)
        assert result["recommendation"] == "Make"
        assert result["confidence"] in ("Low", "Medium")

    def test_buy_medium_variance(self, add_agent_to_path):
        from tools import classify_make_buy
        result = classify_make_buy("MAT-003", cost_variance_percent=10.0, has_multiple_suppliers=True)
        assert result["recommendation"] == "Buy"
        assert result["confidence"] == "Medium"

    def test_buy_high_variance_multi_supplier(self, add_agent_to_path):
        from tools import classify_make_buy
        result = classify_make_buy("MAT-004", cost_variance_percent=22.0, has_multiple_suppliers=True, strategic_importance=False)
        assert result["recommendation"] == "Buy"
        assert result["confidence"] == "High"

    def test_strategic_buy_single_supplier(self, add_agent_to_path):
        from tools import classify_make_buy
        result = classify_make_buy("MAT-005", cost_variance_percent=25.0, has_multiple_suppliers=False, strategic_importance=False)
        assert result["recommendation"] == "Strategic Buy"
        assert result["confidence"] == "High"

    def test_strategic_buy_strategic_importance(self, add_agent_to_path):
        from tools import classify_make_buy
        result = classify_make_buy("MAT-006", cost_variance_percent=22.0, has_multiple_suppliers=True, strategic_importance=True)
        assert result["recommendation"] == "Strategic Buy"

    def test_classification_result_structure(self, add_agent_to_path):
        from tools import classify_make_buy
        result = classify_make_buy("MAT-007", cost_variance_percent=8.0)
        assert "materialNumber" in result
        assert "recommendation" in result
        assert "confidence" in result
        assert "reasoning" in result
        assert "costVariancePercent" in result
        assert result["recommendation"] in ("Make", "Buy", "Strategic Buy")
        assert result["confidence"] in ("Low", "Medium", "High")


class TestBomTools:
    """Tests for the 7 BOM analysis tools in IBD_TESTING mode."""

    @pytest.mark.asyncio
    async def test_get_bom_cross_plant(self, add_agent_to_path):
        from tools import get_bom_cross_plant
        result_str = await get_bom_cross_plant("NEXA-001", "MANE", "GURG")
        result = json.loads(result_str)
        assert result["status"] == "success"
        assert result["vehicleMaterial"] == "NEXA-001"
        assert result["plant1"] == "MANE"
        assert result["plant2"] == "GURG"
        assert len(result["components"]) >= 3
        # Verify component structure
        comp = result["components"][0]
        assert "materialNumber" in comp
        assert "costPlant1" in comp
        assert "costPlant2" in comp
        assert "costVarianceAbsoluteINR" in comp
        assert "costVariancePercent" in comp

    @pytest.mark.asyncio
    async def test_get_material_procurement_type(self, add_agent_to_path):
        from tools import get_material_procurement_type
        result_str = await get_material_procurement_type("NEXA-ENG-001", "MANE")
        result = json.loads(result_str)
        assert result["materialNumber"] == "NEXA-ENG-001"
        assert result["plant"] == "MANE"
        assert result["procurementType"] in ("E", "F", "X")

    @pytest.mark.asyncio
    async def test_get_purchase_info_records(self, add_agent_to_path):
        from tools import get_purchase_info_records
        result_str = await get_purchase_info_records("NEXA-BRAKE-003", "MANE")
        result = json.loads(result_str)
        assert result["materialNumber"] == "NEXA-BRAKE-003"
        assert "hasMultipleSuppliers" in result
        assert isinstance(result["infoRecords"], list)
        assert len(result["infoRecords"]) >= 1

    @pytest.mark.asyncio
    async def test_get_purchase_info_records_single_supplier(self, add_agent_to_path):
        from tools import get_purchase_info_records
        result_str = await get_purchase_info_records("NEXA-ENG-001", "MANE")
        result = json.loads(result_str)
        # ENG materials have single supplier in mock
        assert result["hasMultipleSuppliers"] == False
        assert result["vendorCount"] == 1

    @pytest.mark.asyncio
    async def test_get_purchase_order_history(self, add_agent_to_path):
        from tools import get_purchase_order_history
        result_str = await get_purchase_order_history("NEXA-BRAKE-003", "MANE")
        result = json.loads(result_str)
        assert result["materialNumber"] == "NEXA-BRAKE-003"
        assert result["totalOrders"] > 0
        assert "orders" in result
        assert len(result["orders"]) > 0

    @pytest.mark.asyncio
    async def test_get_material_valuation(self, add_agent_to_path):
        from tools import get_material_valuation
        result_str = await get_material_valuation("NEXA-ENG-001", "MANE")
        result = json.loads(result_str)
        assert result["materialNumber"] == "NEXA-ENG-001"
        assert "movingAveragePrice" in result
        assert result["currency"] == "INR"
        assert result["movingAveragePrice"] > 0

    def test_get_bom_tools_count(self, add_agent_to_path):
        from tools import get_bom_tools
        tools = get_bom_tools()
        assert len(tools) == 7

    def test_get_bom_tool_names(self, add_agent_to_path):
        from tools import get_bom_tools
        tools = get_bom_tools()
        names = {t.name for t in tools}
        assert "get_bom_cross_plant" in names
        assert "get_material_procurement_type" in names
        assert "get_purchase_info_records" in names
        assert "get_purchase_order_history" in names
        assert "get_material_valuation" in names
        assert "trigger_procurement_review_workflow" in names
        assert "trigger_costing_review_workflow" in names


class TestMcpToolsMockMode:
    """Tests for MCP tool loading in mock mode (IBD_TESTING=1)."""

    @pytest.mark.asyncio
    async def test_get_mcp_tools_mock_no_mock_file(self, add_agent_to_path):
        """When mcp-mock.json doesn't exist, get_mcp_tools returns empty list."""
        from mcp_tools import get_mcp_tools
        tools = await get_mcp_tools(user_token=None)
        # Returns empty list if no mcp-mock.json
        assert isinstance(tools, list)

    def test_set_and_get_user_token(self, add_agent_to_path):
        from mcp_tools import set_user_token, get_user_token
        token_ctx = set_user_token("test-token-123")
        assert get_user_token() == "test-token-123"
        set_user_token(None)
        assert get_user_token() is None
