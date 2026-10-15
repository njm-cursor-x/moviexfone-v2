from __future__ import annotations

import json

from moviexfone.paths import data_dir
from moviexfone.sources.parse import parse_alamo, parse_assemble, parse_nitehawk_html


def test_alamo_fixture_drops_sold_out_and_past() -> None:
    payload = json.loads((data_dir() / "fixtures" / "alamo_nyc.json").read_text(encoding="utf-8"))
    rows = parse_alamo(payload)
    assert rows
    assert all(row.source == "alamo_json" for row in rows)
    assert all(row.status == "on_sale" for row in rows)
    formats = {row.format_norm for row in rows}
    assert "35mm" in formats
    assert "standard" in formats
    assert all(
        row.ticket_url and row.ticket_url.startswith("https://drafthouse.com/") for row in rows
    )
    assert payload["_meta"]["synthetic"] is True


def test_assemble_fixture_has_no_format_and_keeps_ticket_links() -> None:
    payload = json.loads(
        (data_dir() / "fixtures" / "assemble_screenings.json").read_text(encoding="utf-8")
    )
    rows = parse_assemble(payload)
    assert len(rows) >= 10
    assert {row.format_norm for row in rows} == {"unknown"}
    assert all(row.ticket_url and row.ticket_url.startswith("https://") for row in rows)


def test_assemble_nested_shape() -> None:
    payload = {
        "films": [
            {
                "title": "Verity",
                "screenings": [
                    {
                        "venue_name": "Regal Union Square",
                        "starts_at": "2026-10-13T19:35:00-04:00",
                        "tickets_url": "https://www.regmovies.com/verity",
                        "status": "on_sale",
                    }
                ],
            }
        ]
    }
    rows = parse_assemble(payload)
    assert len(rows) == 1
    assert rows[0].movie_title == "Verity"
    assert rows[0].ticket_url == "https://www.regmovies.com/verity"


def test_nitehawk_jsonld() -> None:
    html = (data_dir() / "fixtures" / "nitehawk_prospectpark.html").read_text(encoding="utf-8")
    rows = parse_nitehawk_html(html, default_theater="Nitehawk Cinema Prospect Park")
    assert len(rows) == 1
    assert rows[0].movie_title == "The Social Reckoning"
    assert rows[0].ticket_url is not None
    assert "nitehawkcinema.com" in rows[0].ticket_url


def test_bad_ticket_url_is_dropped() -> None:
    rows = parse_assemble(
        {
            "screenings": [
                {
                    "film": "Verity",
                    "venue": "Quad Cinema",
                    "start": "2026-10-13T19:00:00-04:00",
                    "tickets_url": "javascript:alert(1)",
                    "availability": "on_sale",
                }
            ]
        }
    )
    assert rows[0].ticket_url is None
