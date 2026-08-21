import httpx
from typing import Optional
from app.models.media import BaseMediaCard, MediaType

ANILIST_URL = "https://graphql.anilist.co"

# GraphQL Query to get top trending anime
TRENDING_QUERY = """
query {
  Page(page: 1, perPage: 20) {
    media(type: ANIME, sort: TRENDING_DESC) {
      id
      title { romaji english }
      coverImage { extraLarge }
      bannerImage
      seasonYear
      averageScore
    }
  }
}
"""

async def fetch_anilist_trending() -> list:
    """Fetches trending anime via GraphQL and formats to Unified Model."""
    async with httpx.AsyncClient() as client:
        resp = await client.post(ANILIST_URL, json={"query": TRENDING_QUERY})
        if resp.status_code != 200:
            return []
            
        data = resp.json().get("data", {}).get("Page", {}).get("media", [])
        
        formatted_list = []
        for item in data:
            title = item.get("title", {})
            best_title = title.get("english") or title.get("romaji")
            score = item.get("averageScore")
            
            card = BaseMediaCard(
                id=f"a{item.get('id')}", # Custom AniList prefix
                title=best_title,
                type=MediaType.ANIME,
                poster_url=item.get("coverImage", {}).get("extraLarge"),
                banner_url=item.get("bannerImage"),
                release_year=item.get("seasonYear"),
                rating=round(score / 10, 1) if score else None # AniList is out of 100
            )
            formatted_list.append(card.model_dump())
            
        return formatted_list
    
SEARCH_QUERY = """
query($search: String) {
  Page(page: 1, perPage: 15) {
    media(search: $search, type: ANIME, sort: SEARCH_MATCH) {
      id title { romaji english } coverImage { extraLarge } seasonYear averageScore
    }
  }
}
"""

async def search_anilist(query: str) -> list:
    async with httpx.AsyncClient() as client:
        resp = await client.post(ANILIST_URL, json={"query": SEARCH_QUERY, "variables": {"search": query}})
        if resp.status_code != 200: return []
        
        formatted_list = []
        for item in resp.json().get("data", {}).get("Page", {}).get("media", []):
            title = item.get("title", {})
            score = item.get("averageScore")
            
            card = BaseMediaCard(
                id=f"a{item.get('id')}",
                title=title.get("english") or title.get("romaji"),
                type=MediaType.ANIME,
                poster_url=item.get("coverImage", {}).get("extraLarge"),
                release_year=item.get("seasonYear"),
                rating=round(score / 10, 1) if score else None
            )
            formatted_list.append(card.model_dump())
        return formatted_list


# --- UPGRADED DETAILS QUERY ---
# Now includes duration, characters (cast), staff (director), and recommendations
DETAILS_QUERY = """
query($id: Int, $idMal: Int) {
  Media(id: $id, idMal: $idMal, type: ANIME) {
    id 
    idMal 
    title { romaji english } 
    description 
    status 
    genres
    coverImage { extraLarge } 
    bannerImage 
    seasonYear 
    averageScore 
    duration
    studios(isMain: true) { nodes { name } }
    trailer { id site } 
    characters(sort: ROLE, perPage: 10) {
      edges {
        role
        node {
          name { full }
          image { large }
        }
      }
    }
    staff(perPage: 5) {
      edges {
        role
        node { name { full } }
      }
    }
    recommendations(sort: RATING_DESC, perPage: 12) {
      nodes {
        mediaRecommendation {
          id
          title { english romaji }
          coverImage { extraLarge }
          bannerImage
          seasonYear
          averageScore
          type
        }
      }
    }
  }
}
"""

async def get_anilist_details(anilist_id: int = None, mal_id: int = None) -> dict:
    variables = {}
    if anilist_id: variables["id"] = anilist_id
    elif mal_id: variables["idMal"] = mal_id
        
    async with httpx.AsyncClient(timeout=8.0) as client:
        resp = await client.post(ANILIST_URL, json={"query": DETAILS_QUERY, "variables": variables})
        if resp.status_code != 200: return None
        return resp.json().get("data", {}).get("Media", {})


# ==========================================
# SEASON CHAIN STITCHING
# ==========================================
# AniList has no "seasons" concept — each cour/season of a show is its own,
# disjoint Media node (e.g. "Jujutsu Kaisen" and "Jujutsu Kaisen Season 2"
# are two separate ids). To build a unified season list we walk the
# PREQUEL/SEQUEL relation edges outward from a starting id in both
# directions, restricted to TV/TV_SHORT format so movies, OVAs, and side
# stories don't get counted as numbered seasons.

SEASON_CHAIN_QUERY = """
query($id: Int) {
  Media(id: $id, type: ANIME) {
    id idMal seasonYear episodes format
    title { romaji english }
    relations {
      edges {
        relationType(version: 2)
        node { id idMal format seasonYear episodes title { romaji english } }
      }
    }
  }
}
"""

async def _fetch_relations_node(client: httpx.AsyncClient, anilist_id: int) -> Optional[dict]:
    resp = await client.post(ANILIST_URL, json={"query": SEASON_CHAIN_QUERY, "variables": {"id": anilist_id}})
    if resp.status_code != 200:
        return None
    return resp.json().get("data", {}).get("Media")

def _node_summary(node: dict) -> dict:
    title = node.get("title", {})
    return {
        "anilist_id": node["id"],
        "mal_id": node.get("idMal"),
        "season_year": node.get("seasonYear"),
        "episodes": node.get("episodes"),
        "title": title.get("english") or title.get("romaji"),
    }

def _seasonal_edges(node: dict, relation: str) -> list:
    edges = node.get("relations", {}).get("edges", [])
    return [
        e["node"] for e in edges
        if e.get("relationType") == relation and e.get("node", {}).get("format") in ("TV", "TV_SHORT")
    ]

async def get_anilist_season_chain(start_id: int, max_hops: int = 10) -> list:
    """
    Walks PREQUEL/SEQUEL relations from start_id in both directions to
    assemble an ordered season list: [{anilist_id, mal_id, season_year,
    episodes, title}, ...] in chronological order.

    Returns a single-item list (just the starting node) if the show has no
    TV-format prequels/sequels — callers should treat len() == 1 as "no
    stitching needed, this is a standalone season".
    """
    async with httpx.AsyncClient(timeout=8.0) as client:
        origin = await _fetch_relations_node(client, start_id)
        if not origin:
            return []

        # Walk backwards to find the earliest season in the chain
        earliest, seen = origin, {origin["id"]}
        for _ in range(max_hops):
            prequels = _seasonal_edges(earliest, "PREQUEL")
            if not prequels or prequels[0]["id"] in seen:
                break
            node = await _fetch_relations_node(client, prequels[0]["id"])
            if not node:
                break
            earliest = node
            seen.add(node["id"])

        # Walk forwards from the earliest season to collect the full chain
        chain, current = [earliest], earliest
        for _ in range(max_hops):
            sequels = _seasonal_edges(current, "SEQUEL")
            if not sequels or sequels[0]["id"] in seen:
                break
            node = await _fetch_relations_node(client, sequels[0]["id"])
            if not node:
                break
            chain.append(node)
            seen.add(node["id"])
            current = node

        return [_node_summary(n) for n in chain]