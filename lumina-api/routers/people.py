from __future__ import annotations

from fastapi import APIRouter, Query
from fastapi.responses import PlainTextResponse

from schemas.people import PeopleQuery
from services.people_context import known_people, query_people
from services.people_sync import sync_people

router = APIRouter(prefix="/people", tags=["people"])


@router.get("/list")
def list_people(summary: bool = Query(False)):
    people = known_people()
    if summary:
        if not people:
            return PlainTextResponse("0 known people.")
        names = ", ".join(p["name"] for p in people[:5])
        more = f" +{len(people) - 5} more" if len(people) > 5 else ""
        return PlainTextResponse(f"{len(people)} known people: {names}{more}.")
    return {"count": len(people), "people": people}


@router.post("/query")
def query_people_route(payload: PeopleQuery, summary: bool = Query(False)):
    result = query_people(payload.q, person=payload.person, top_k=payload.top_k)
    if summary:
        hits = result.get("hits") or []
        if not hits:
            return PlainTextResponse(f"0 hits for {result.get('query')!r}.")
        people = ", ".join(dict.fromkeys(hit["display_name"] for hit in hits))
        return PlainTextResponse(f"{len(hits)} hit(s) across {people}.")
    return result


@router.post("/sync")
def sync_people_route():
    result = sync_people()
    return {"status": "ok", **result}
