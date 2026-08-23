import requests
import os
from datetime import datetime, timedelta
try:
    import zoneinfo
except ImportError:
    from backports import zoneinfo # For older Python versions

# AniList GraphQL Query for Schedule
QUERY = """
query($from: Int, $to: Int, $page: Int) {
  Page(page: $page, perPage: 50) {
    pageInfo { hasNextPage }
    airingSchedules(airingAt_greater: $from, airingAt_lesser: $to, sort: TIME) {
      airingAt
      episode
      media {
        id
        title { english romaji }
      }
    }
  }
}
"""

def clear_screen():
    os.system('cls' if os.name == 'nt' else 'clear')

def fetch_schedule_for_day(date_obj: datetime.date, tz_mode: str):
    """Fetches all anime airing within a specific day based on the selected timezone."""
    
    # 1. Determine the Timezone
    if tz_mode == "JST":
        active_tz = zoneinfo.ZoneInfo("Asia/Tokyo")
    else:
        # Use the system's local timezone (e.g., America/Chicago)
        active_tz = datetime.now().astimezone().tzinfo

    # 2. Define the start and end of the day IN THAT TIMEZONE
    start_of_day = datetime(date_obj.year, date_obj.month, date_obj.day, 0, 0, 0, tzinfo=active_tz)
    end_of_day = start_of_day + timedelta(days=1)

    # 3. Convert to UTC Unix Timestamps for AniList
    from_ts = int(start_of_day.timestamp())
    to_ts = int(end_of_day.timestamp())

    print(f"Fetching schedule for {start_of_day.strftime('%A, %B %d, %Y')} ({tz_mode})...")

    # 4. Fetch from AniList with a custom User-Agent (helps bypass some blocks)
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36"
    }

    has_next_page = True
    page = 1
    results = []

    while has_next_page:
        response = requests.post(
            "https://graphql.anilist.co",
            json={"query": QUERY, "variables": {"from": from_ts, "to": to_ts, "page": page}},
            headers=headers,
            timeout=10
        )

        if response.status_code != 200:
            print(f"\n[ERROR] AniList API returned {response.status_code}: {response.text}")
            return []

        data = response.json().get("data", {}).get("Page", {})
        schedules = data.get("airingSchedules", [])
        
        for item in schedules:
            media = item.get("media", {})
            title_dict = media.get("title", {})
            # Prioritize English, fallback to Romaji
            title = title_dict.get("english") or title_dict.get("romaji") or "Unknown Title"
            
            # Format time in the selected timezone
            air_dt = datetime.fromtimestamp(item["airingAt"], tz=zoneinfo.ZoneInfo("UTC")).astimezone(active_tz)
            time_str = air_dt.strftime("%I:%M %p")
            
            results.append({
                "time": time_str,
                "title": title,
                "episode": item.get("episode")
            })

        has_next_page = data.get("pageInfo", {}).get("hasNextPage", False)
        page += 1

    return results

def main():
    current_date = datetime.now().date()
    tz_mode = "LOCAL" # Can be "LOCAL" or "JST"

    while True:
        clear_screen()
        print("="*60)
        print(f" ANILIST SCHEDULE EXPLORER  |  Mode: {tz_mode}")
        print("="*60)
        
        schedule = fetch_schedule_for_day(current_date, tz_mode)
        
        print("\n" + "-"*60)
        if not schedule:
            print(" No episodes found for this day (or API error occurred).")
        else:
            for item in schedule:
                print(f"[{item['time']}] {item['title']} (Ep {item['episode']})")
        print("-" * 60)
        
        print("\nControls:")
        print(" [N] Next Day    [P] Previous Day")
        print(" [T] Toggle Timezone (Local / JST)")
        print(" [Q] Quit")
        
        choice = input("\nSelect an option: ").strip().upper()
        
        if choice == 'N':
            current_date += timedelta(days=1)
        elif choice == 'P':
            current_date -= timedelta(days=1)
        elif choice == 'T':
            tz_mode = "JST" if tz_mode == "LOCAL" else "LOCAL"
        elif choice == 'Q':
            break

if __name__ == "__main__":
    main()