"""Explicitly fictional records, only loaded by a user-initiated demo action."""

from .sources import SourceSnapshot


def demo_snapshot():
    entities = [
        {"id": "demo-person-001", "schema": "Person", "caption": "Alex Morgan — FICTIONAL DEMO",
         "datasets": ["fictional_demo"], "properties": {
             "name": ["Alex Morgan — FICTIONAL DEMO"], "alias": ["A. Morgan (fictional)"],
             "topics": ["wanted"], "nationality": ["us"], "birthDate": ["1985"],
             "notes": ["Invented training record. This is not a real person or a real wanted notice."],
             "status": ["Fictional notice used to demonstrate source status"],
         }},
        {"id": "demo-company-002", "schema": "Company", "caption": "Northstar Example Trading — FICTIONAL DEMO",
         "datasets": ["fictional_demo"], "properties": {
             "name": ["Northstar Example Trading — FICTIONAL DEMO"], "topics": ["sanction"],
             "jurisdiction": ["gb"], "notes": ["Invented company, not an actual sanctions target."],
         }},
        {"id": "demo-sanction-003", "schema": "Sanction", "caption": "Fictional example listing",
         "datasets": ["fictional_demo"], "properties": {
             "entity": ["demo-company-002"], "authority": ["Fictional demonstration authority"],
             "program": ["DEMO ONLY"], "reason": ["Tests the FollowTheMoney entity reference"],
             "listingDate": ["2025-01-01"],
         }},
        {"id": "demo-person-004", "schema": "Person", "caption": "Jordan Rivera — FICTIONAL DEMO",
         "datasets": ["fictional_demo"], "properties": {
             "name": ["Jordan Rivera — FICTIONAL DEMO"], "topics": ["role.pep"],
             "position": ["Fictional public office"],
             "notes": ["PEP status by itself does not indicate wrongdoing. This record is fictional."],
         }},
        {"id": "demo-person-005", "schema": "Person", "caption": "Taylor Chen — FICTIONAL DEMO",
         "datasets": ["fictional_demo"], "properties": {
             "name": ["Taylor Chen — FICTIONAL DEMO"], "classification": ["Missing person"],
             "notes": ["Fictional missing-person notice. This person is not classified as wanted."],
         }},
    ]
    return SourceSnapshot(source="demo", entities=entities, complete=True,
                          metadata={"label": "Fictional demonstration", "license": "Fictional test data",
                                    "demo": True, "source_url": "", "attribution": "EL JEFFE example records"})
