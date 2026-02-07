import os
import json
import yaml
import requests
import threading
from pathlib import Path
import concurrent.futures
from bs4 import BeautifulSoup
from typing import Dict, List
from urllib.parse import urlencode
from datetime import datetime, timezone
from collections import Counter, defaultdict
from jinja2 import Environment, FileSystemLoader


session = requests.Session()
session.headers.update({"User-Agent": "Mozilla/5.0"})


def load_settings():
    config_path = Path("config/settings.yml")

    with open(config_path, "r") as f:
        cfg = yaml.safe_load(f)

    cfg.setdefault("clickhouse", {})
    cfg.setdefault("processing", {})
    cfg.setdefault("paths", {})

    ch = cfg["clickhouse"]
    pr = cfg["processing"]

    ch["url"] = os.getenv("CLICKHOUSE_URL") or ch.get(
        "url", "https://play.clickhouse.com"
    )
    ch["table"] = os.getenv("CLICKHOUSE_TABLE") or ch.get("table", "github_events")
    ch["timeout"] = float(os.getenv("CLICKHOUSE_TIMEOUT", ch.get("timeout", 60)))

    pr["recent_repos_limit"] = (
        int(os.getenv("RECENT_REPOS_LIMIT", str(pr.get("recent_repos_limit"))))
        if pr.get("recent_repos_limit") is not None
        else None
    )

    pr["top_n"] = (
        int(os.getenv("TOP_N", str(pr.get("top_n"))))
        if pr.get("top_n") is not None
        else None
    )

    pr["max_workers"] = int(os.getenv("MAX_WORKERS", pr.get("max_workers", 4)))

    cfg["user"] = {"login": os.getenv("GH_USER")}
    if not cfg["user"]["login"]:
        raise RuntimeError("GitHub username missing. Set GH_USER env var.")

    return cfg


settings = load_settings()


RECOMMENDATIONS_DIR = Path(settings["paths"]["recommendations_dir"])
LATEST_JSON = Path(settings["paths"]["latest_json"])
TEMPLATES_DIR = Path("templates")

OUTPUT_HTML = Path("index.html")
OVERVIEW_HTML = Path("overview.html")
TAGCLOUD_HTML = Path("tagcloud.html")


CLICKHOUSE_URL = settings["clickhouse"]["url"]
CLICKHOUSE_TABLE = settings["clickhouse"]["table"]
CLICKHOUSE_TIMEOUT = settings["clickhouse"]["timeout"]

RECENT_REPOS_LIMIT = settings["processing"]["recent_repos_limit"]
MAX_WORKERS = settings["processing"]["max_workers"]
TOP_N = settings["processing"]["top_n"]

USER_LOGIN = settings["user"]["login"]


progress_lock = threading.Lock()
progress_counter = 0

metadata_progress_lock = threading.Lock()
metadata_progress = 0


metadata_cache: Dict[str, dict] = {}
metadata_lock = threading.Lock()


def get_repo_metadata(url: str):
    with metadata_lock:
        cached = metadata_cache.get(url)

    if cached:
        return cached

    html = session.get(url).text
    soup = BeautifulSoup(html, "html.parser")

    desc_tag = soup.select_one("p.f4.my-3")
    description = desc_tag.text.strip() if desc_tag else None

    topics = [t.text.strip() for t in soup.select("a.topic-tag.topic-tag-link")]

    languages = {}

    for cell in soup.select("div.BorderGrid-cell"):
        header = cell.select_one("h2")

        if header and header.get_text(strip=True).lower() == "languages":
            lang_list = cell.select_one("ul.list-style-none")

            if not lang_list:
                continue

            for li in lang_list.select("li"):
                name_tag = li.select_one("span.color-fg-default.text-bold")
                percent_tag = name_tag.find_next("span") if name_tag else None

                if name_tag and percent_tag:
                    languages[name_tag.text.strip()] = percent_tag.text.strip()

    data = {"description": description, "topics": topics, "languages": languages}

    with metadata_lock:
        metadata_cache[url] = data

    return data


def fetch_metadata_for_repo_names(repo_names: List[str]):
    if not repo_names:
        return {}

    total = len(repo_names)
    metadata_map: Dict[str, dict] = {}

    with concurrent.futures.ThreadPoolExecutor(MAX_WORKERS) as executor:
        future_by_name = {
            executor.submit(get_repo_metadata, f"https://github.com/{name}"): name
            for name in repo_names
        }

        for future in concurrent.futures.as_completed(future_by_name):
            repo_name = future_by_name[future]

            global metadata_progress
            with metadata_progress_lock:
                metadata_progress += 1
                print(f"[META {metadata_progress}/{total}] {repo_name}")

            metadata_map[repo_name] = future.result()

    return metadata_map


class ClickHouseError(RuntimeError):
    pass


def execute_clickhouse(sql: str):
    params = {"default_format": "JSONEachRow", "user": "explorer"}
    url = f"{CLICKHOUSE_URL}/?{urlencode(params)}"

    for attempt in range(5):
        try:
            r = session.post(url, data=sql.encode(), timeout=CLICKHOUSE_TIMEOUT)

            if r.status_code != 200:
                raise ClickHouseError(r.text)

            return [json.loads(x) for x in r.text.splitlines() if x.strip()]
        except Exception as e:
            if attempt == 4:
                raise e

    return []


def sql_literal(x: str):
    return "'" + x.replace("\\", "\\\\").replace("'", "\\'") + "'"


def fetch_user_starred_repos(username: str):
    repos = []
    page = 1

    while True:
        url = (
            f"https://api.github.com/users/{username}/starred?per_page=100&page={page}"
        )
        r = session.get(url, timeout=20)

        if r.status_code != 200:
            raise RuntimeError(r.text)

        batch = r.json()

        if not batch:
            break

        repos.extend(repo["full_name"] for repo in batch)
        page += 1
    return repos


def fetch_repo_event_counts(repos: List[str], event: str):
    if not repos:
        return {}

    sql = f"""
        SELECT repo_name, count() AS total
        FROM {CLICKHOUSE_TABLE}
        WHERE event_type={sql_literal(event)}
          AND repo_name IN ({", ".join(sql_literal(r) for r in repos)})
        GROUP BY repo_name
    """
    return {r["repo_name"]: int(r["total"]) for r in execute_clickhouse(sql)}


def build_recommendations_for_repo(repo: str, total: int):
    global progress_counter

    with progress_lock:
        progress_counter += 1

        print(f"[{progress_counter}/{total}] Processing {repo}")

    limit_clause = "" if TOP_N is None else f"LIMIT {TOP_N}"

    sql = f"""
        SELECT 
            e.repo_name AS neighbor_repo,
            countDistinct(e.actor_login) AS forkers
        FROM {CLICKHOUSE_TABLE} e
        INNER JOIN (
            SELECT DISTINCT actor_login
            FROM {CLICKHOUSE_TABLE}
            WHERE event_type='ForkEvent'
              AND repo_name={sql_literal(repo)}
        ) s USING actor_login
        WHERE e.event_type='ForkEvent'
          AND e.repo_name != {sql_literal(repo)}
        GROUP BY neighbor_repo
        ORDER BY forkers DESC
        {limit_clause}
    """

    rows = execute_clickhouse(sql)
    recs = [{"repo": r["neighbor_repo"], "count": int(r["forkers"])} for r in rows]

    return {"repo": repo, "recommendations": recs}


def load_existing_recommendations():
    """Load existing recommendations from LATEST_JSON if it exists."""
    if not LATEST_JSON.exists():
        return {}

    try:
        data = json.loads(LATEST_JSON.read_text())
        return {r["repo"]: r for r in data.get("results", [])}
    except Exception:
        return {}


def save_json_output(username: str, results, generated_at: datetime):
    RECOMMENDATIONS_DIR.mkdir(parents=True, exist_ok=True)
    LATEST_JSON.write_text(
        json.dumps(
            {
                "generated_at": generated_at.isoformat(),
                "username": username,
                "results": results,
            },
            indent=2,
        )
    )


def compact_number(value):
    if value is None:
        return "0"

    num = float(value)
    units = ["", "K", "M", "B", "T"]
    magnitude = 0

    while abs(num) >= 1000 and magnitude < len(units) - 1:
        num /= 1000.0
        magnitude += 1

    if magnitude == 0:
        return f"{int(num):,}"

    formatted = f"{num:.1f}".rstrip("0").rstrip(".")
    return f"{formatted}{units[magnitude]}"


def parse_percent(value: str):
    if not value:
        return 0.0

    try:
        return float(str(value).replace("%", "").strip())
    except ValueError:
        return 0.0


def build_search_index(results):
    items = []

    for group_index, group in enumerate(results):
        group_metadata = group.get("metadata") or {}
        items.append(
            {
                "type": "source",
                "repo": group.get("repo", ""),
                "description": group_metadata.get("description") or "",
                "topics": group_metadata.get("topics") or [],
                "group_index": group_index,
                "parent_repo": group.get("repo", ""),
                "anchor": f"source-{group_index}",
            }
        )

        for rec_index, rec in enumerate(group.get("recommendations", [])):
            rec_metadata = rec.get("metadata") or {}
            items.append(
                {
                    "type": "recommendation",
                    "repo": rec.get("repo", ""),
                    "description": rec_metadata.get("description") or "",
                    "topics": rec_metadata.get("topics") or [],
                    "group_index": group_index,
                    "parent_repo": group.get("repo", ""),
                    "anchor": f"rec-{group_index}-{rec_index}",
                }
            )

    return items


def build_analytics(results):
    recommendation_counter = Counter()
    language_totals = defaultdict(float)

    total_recommendations = sum(len(r.get("recommendations", [])) for r in results)
    distinct_recommendations = len(
        {rec["repo"] for group in results for rec in group.get("recommendations", [])}
    )

    for group in results:
        group_languages = (group.get("metadata") or {}).get("languages") or {}
        for language, pct in group_languages.items():
            language_totals[language] += parse_percent(pct)

        for rec in group.get("recommendations", []):
            recommendation_counter[rec["repo"]] += int(rec.get("count", 0))

            rec_languages = (rec.get("metadata") or {}).get("languages") or {}
            for language, pct in rec_languages.items():
                language_totals[language] += parse_percent(pct)

    top_languages = sorted(language_totals.items(), key=lambda x: x[1], reverse=True)[
        :8
    ]
    top_recommendations = recommendation_counter.most_common(10)

    avg_per_repo = 0
    if results:
        avg_per_repo = round(total_recommendations / len(results), 1)

    return {
        "avg_recommendations_per_repo": avg_per_repo,
        "distinct_recommendations": distinct_recommendations,
        "language_breakdown": [
            {"language": language, "value": round(value, 2)}
            for language, value in top_languages
        ],
        "top_recommendations": [
            {"repo": repo, "overlap": overlap} for repo, overlap in top_recommendations
        ],
    }


def build_client_results(results):
    client_results = []

    for group in results:
        group_metadata = group.get("metadata") or {}
        group_item = {
            "repo": group.get("repo", ""),
            "total_stars": int(group.get("total_stars") or 0),
            "total_forks": int(group.get("total_forks") or 0),
            "metadata": {
                "description": group_metadata.get("description") or "",
                "topics": group_metadata.get("topics") or [],
                "languages": group_metadata.get("languages") or {},
            },
            "recommendations": [],
        }

        for rec in group.get("recommendations", []):
            rec_metadata = rec.get("metadata") or {}
            group_item["recommendations"].append(
                {
                    "repo": rec.get("repo", ""),
                    "count": int(rec.get("count") or 0),
                    "total_stars": int(rec.get("total_stars") or 0),
                    "total_forks": int(rec.get("total_forks") or 0),
                    "metadata": {
                        "description": rec_metadata.get("description") or "",
                        "topics": rec_metadata.get("topics") or [],
                        "languages": rec_metadata.get("languages") or {},
                    },
                }
            )

        client_results.append(group_item)

    return client_results


def render_html_output(username: str, results, generated_at: datetime):
    env = Environment(loader=FileSystemLoader(str(TEMPLATES_DIR)))

    env.filters["compact"] = compact_number

    total_recs = sum(len(r["recommendations"]) for r in results)
    repo_count = len(results)
    analytics = build_analytics(results)
    data_url = (
        LATEST_JSON.as_posix()
        if not LATEST_JSON.is_absolute()
        else "recommendations/latest.json"
    )

    context = {
        "username": username,
        "results": results,
        "generated_at": generated_at,
        "generated_at_display": generated_at.strftime("%Y-%m-%d %H:%M UTC"),
        "repo_count": repo_count,
        "total_recommendations": total_recs,
        "analytics": analytics,
        "analytics_json": json.dumps(analytics, separators=(",", ":")),
        "data_url": data_url,
    }

    # Generate main recommendations page
    template = env.get_template("index.html")
    OUTPUT_HTML.write_text(template.render(**context))

    # Generate overview page
    overview_template = env.get_template("overview.html")
    OVERVIEW_HTML.write_text(overview_template.render(**context))

    # Generate tag cloud page
    tagcloud_template = env.get_template("tagcloud.html")
    TAGCLOUD_HTML.write_text(tagcloud_template.render(**context))

    print(
        f"[INFO] Generated 3 HTML pages: {OUTPUT_HTML}, {OVERVIEW_HTML}, {TAGCLOUD_HTML}"
    )


def main():
    repos = fetch_user_starred_repos(USER_LOGIN)
    if RECENT_REPOS_LIMIT:
        repos = repos[:RECENT_REPOS_LIMIT]

    existing_recs = load_existing_recommendations()
    existing_repos = set(existing_recs.keys())

    new_repos = [r for r in repos if r not in existing_repos]

    generated_at = datetime.now(timezone.utc)

    if new_repos:
        print(f"[INFO] Found {len(new_repos)} new repositories to process...")

        print("[INFO] Fetching repo statistics in batch...")
        main_star_counts = fetch_repo_event_counts(repos, "WatchEvent")
        main_fork_counts = fetch_repo_event_counts(repos, "ForkEvent")

        print("[INFO] Generating recommendations for new repos...")
        with concurrent.futures.ThreadPoolExecutor(MAX_WORKERS) as ex:
            results = list(
                ex.map(
                    lambda r: build_recommendations_for_repo(r, len(new_repos)),
                    new_repos,
                )
            )
    else:
        print("[INFO] No new repositories found. Skipping recommendation generation.")
        results = []
        main_star_counts = {}
        main_fork_counts = {}

    all_results = existing_recs.copy()
    for new_result in results:
        all_results[new_result["repo"]] = new_result

    final_results = list(all_results.values())

    all_recommended = [
        r["repo"] for repo in final_results for r in repo["recommendations"]
    ]
    rec_star_counts = fetch_repo_event_counts(all_recommended, "WatchEvent")
    rec_fork_counts = fetch_repo_event_counts(all_recommended, "ForkEvent")

    repo_names = {entry["repo"] for entry in final_results}
    for repo_entry in final_results:
        repo_names.update(rec["repo"] for rec in repo_entry["recommendations"])

    if repo_names:
        print(
            f"[INFO] Fetching metadata for {len(repo_names)} repos using {MAX_WORKERS} workers..."
        )
        metadata_map = fetch_metadata_for_repo_names(list(repo_names))
    else:
        metadata_map = {}

    for repo_entry in final_results:
        repo_name = repo_entry["repo"]
        repo_entry["metadata"] = metadata_map.get(repo_name, {})
        repo_entry["total_stars"] = main_star_counts.get(repo_name, 0)
        repo_entry["total_forks"] = main_fork_counts.get(repo_name, 0)

        if not repo_entry["recommendations"]:
            repo_entry["recommendations"] = [
                {
                    "repo": repo_name,
                    "count": repo_entry["total_forks"],
                    "metadata": repo_entry["metadata"],
                    "total_stars": repo_entry["total_stars"],
                    "total_forks": repo_entry["total_forks"],
                    "score": 0.0,
                    "non_normalized": True,
                }
            ]

        for rec in repo_entry["recommendations"]:
            rec_name = rec["repo"]

            if "metadata" not in rec:
                rec["metadata"] = metadata_map.get(rec_name, {})
            if "total_stars" not in rec:
                rec["total_stars"] = rec_star_counts.get(rec_name, 0)
            if "total_forks" not in rec:
                rec["total_forks"] = rec_fork_counts.get(rec_name, 0)
            ts = rec["total_stars"]
            rec["score"] = round(rec["count"] / ts, 6) if ts else 0.0

    save_json_output(USER_LOGIN, final_results, generated_at)
    render_html_output(USER_LOGIN, final_results, generated_at)
    print("[DONE]")


if __name__ == "__main__":
    main()
