"""
Direct BOM API test — run on CF via: cf ssh bom-cost-optimization-agent
Or locally if VCAP_SERVICES is set.
Tests every possible way to get BOM items for DEMOCAR / 00000381 / plant 1710.
"""
import asyncio, json, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'app'))

from s4hana_client import Client

BOM_ROOT     = "/sap/opu/odata/sap/API_BILL_OF_MATERIAL_SRV_0002"
BOM_ROOT_ALT = "/sap/opu/odata/sap/API_BILL_OF_MATERIAL_SRV"

async def main():
    client = Client()
    BOM_NO  = "00000381"
    PLANT   = "1710"
    CAT     = "M"

    print("=" * 70)
    print("TEST 1: $expand on A_BillOfMaterial (fetch header + items together)")
    print("=" * 70)
    for svc in (BOM_ROOT, BOM_ROOT_ALT):
        for usage in (None, "1", "2", "3"):
            fparts = [f"Material eq 'DEMOCAR'", f"Plant eq '{PLANT}'"]
            if usage:
                fparts.append(f"BOMUsage eq '{usage}'")
            r = await client.get(f"{svc}/A_BillOfMaterial", params={
                "$filter": " and ".join(fparts),
                "$expand": "to_BillOfMaterialItem",
                "$top": "10", "$format": "json"
            })
            hdrs = r.get("results", []) if not r.get("error") else []
            err  = r.get("message","")[:120] if r.get("error") else ""
            print(f"  svc={svc.split('/')[-1]} usage={usage} headers={len(hdrs)} err={err}")
            for h in hdrs:
                nav = h.get("to_BillOfMaterialItem", {})
                if isinstance(nav, dict): nav = nav.get("results", [])
                print(f"    BOM={h.get('BillOfMaterial')} var={h.get('BillOfMaterialVariant')} "
                      f"ver={h.get('BillOfMaterialVersion')} items={len(nav)}")
                for itm in nav[:5]:
                    print(f"      COMPONENT: {itm.get('BOMItemComponent')} qty={itm.get('BOMItemQuantity')} "
                          f"unit={itm.get('BOMItemQuantityUnit')}")

    print()
    print("=" * 70)
    print("TEST 2: Navigation property with all key combos")
    print("=" * 70)
    for svc in (BOM_ROOT, BOM_ROOT_ALT):
        for var in ("1", "01", ""):
            for ver in ("", "0", "00", " "):
                nav_path = (f"/A_BillOfMaterial("
                            f"BillOfMaterial='{BOM_NO}',"
                            f"BillOfMaterialCategory='{CAT}',"
                            f"BillOfMaterialVariant='{var}',"
                            f"BillOfMaterialVersion='{ver}'"
                            f")/to_BillOfMaterialItem")
                r = await client.get(f"{svc}{nav_path}", params={"$top": "50", "$format": "json"})
                items = r.get("results", []) if not r.get("error") else []
                sc    = r.get("status_code","")
                err   = r.get("message","")[:80] if r.get("error") else "OK"
                print(f"  svc={svc.split('/')[-1]} var={repr(var)} ver={repr(ver)} "
                      f"items={len(items)} http={sc} {err}")
                for itm in items[:5]:
                    print(f"    COMPONENT: {itm.get('BOMItemComponent')} qty={itm.get('BOMItemQuantity')}")

    print()
    print("=" * 70)
    print("TEST 3: A_BillOfMaterialItem $filter")
    print("=" * 70)
    for svc in (BOM_ROOT, BOM_ROOT_ALT):
        for filt in [
            f"BillOfMaterial eq '{BOM_NO}'",
            f"BillOfMaterial eq '{BOM_NO}' and BillOfMaterialCategory eq '{CAT}'",
            f"BillOfMaterial eq '{BOM_NO}' and BillOfMaterialCategory eq '{CAT}' and BillOfMaterialVariant eq '1'",
            f"BillOfMaterial eq '{BOM_NO}' and BillOfMaterialCategory eq '{CAT}' and BillOfMaterialVariant eq '01'",
        ]:
            r = await client.get(f"{svc}/A_BillOfMaterialItem",
                                  params={"$filter": filt, "$top": "50", "$format": "json"})
            items = r.get("results", []) if not r.get("error") else []
            err   = r.get("message","")[:100] if r.get("error") else "OK"
            sc    = r.get("status_code","")
            print(f"  svc={svc.split('/')[-1]} items={len(items)} http={sc} err={err}")
            print(f"    filter: {filt}")
            for itm in items[:5]:
                print(f"    COMPONENT: {itm.get('BOMItemComponent')} qty={itm.get('BOMItemQuantity')}")

    print()
    print("=" * 70)
    print("TEST 4: Check $metadata — what entity sets exist?")
    print("=" * 70)
    for svc in (BOM_ROOT, BOM_ROOT_ALT):
        entities = await client.get_service_entities(svc)
        print(f"  {svc.split('/')[-1]}: {entities}")

    print()
    print("=" * 70)
    print("TEST 5: Raw A_BillOfMaterialItem — no filter, just top=5")
    print("=" * 70)
    for svc in (BOM_ROOT, BOM_ROOT_ALT):
        r = await client.get(f"{svc}/A_BillOfMaterialItem",
                              params={"$top": "5", "$format": "json"})
        items = r.get("results", []) if not r.get("error") else []
        err   = r.get("message","")[:150] if r.get("error") else "OK"
        print(f"  svc={svc.split('/')[-1]} items={len(items)} err={err}")
        for itm in items[:3]:
            print(f"    sample keys: {list(itm.keys())[:8]}")
            print(f"    BOM={itm.get('BillOfMaterial')} comp={itm.get('BOMItemComponent')}")

asyncio.run(main())
