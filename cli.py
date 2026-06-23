"""
whenever CLI — runs the same best-value flight search as the web UI.

Usage examples:
  whenever --from Toronto --country China --dep-start 2026-12-12 --ret-start 2027-01-04
  whenever --from YYZ --city Shanghai --adults 2 --child 11 --child 9 \\
           --dep-start 2026-12-12 --dep-span 4 --ret-start 2027-01-04 --ret-span 4
"""
import argparse
import sys

import app


def _build_parser():
    p = argparse.ArgumentParser(
        prog="whenever",
        description="Best-value flexible-date flight search (same logic as the web UI).",
    )
    p.add_argument("--from", dest="origin", required=True,
                   help="City name, or a 3-letter UPPERCASE IATA code used as-is (e.g. Toronto or YYZ).")

    dest_group = p.add_mutually_exclusive_group(required=True)
    dest_group.add_argument("--country",
                            help="Destination country — expanded to top cities via the model.")
    dest_group.add_argument("--city",
                            help="Single destination city name, or a 3-letter UPPERCASE IATA code used as-is.")

    p.add_argument("--max-cities", type=int, default=6,
                   help="Max cities when using --country (default: 6).")
    p.add_argument("--adults", type=int, default=2, help="Number of adults (default: 2).")
    p.add_argument("--child", dest="children", type=int, action="append", default=[],
                   metavar="AGE", help="Child age (repeatable, e.g. --child 11 --child 9).")
    p.add_argument("--dep-start", required=True, help="Departure window start date (ISO, YYYY-MM-DD).")
    p.add_argument("--dep-span", type=int, default=4, help="Number of departure dates (default: 4).")
    p.add_argument("--ret-start", required=True, help="Return window start date (ISO, YYYY-MM-DD).")
    p.add_argument("--ret-span", type=int, default=4, help="Number of return dates (default: 4).")
    p.add_argument("--nonstop-threshold", type=float, default=25,
                   help="Max %% premium to prefer nonstop over cheapest (default: 25).")
    p.add_argument("--families", type=int, default=1, help="Number of families (default: 1).")
    return p


def _resolve_origin(raw: str) -> str:
    """Return an IATA code for the origin.
    Input is used as-is only when it is exactly 3 uppercase ASCII letters (e.g. YYZ).
    Anything else (mixed-case, longer, shorter) is resolved via app.resolve_airport.
    """
    if raw.isalpha() and raw.isupper() and len(raw) == 3:
        return raw
    return app.resolve_airport(raw)


def _render(result: dict) -> str:
    """Render the search result as plain-text matrix + recommendation."""
    lines = []
    dep_dates = result["dep_dates"]
    ret_dates = result["ret_dates"]
    families = result.get("families", 1)

    for r in result["results"]:
        lines.append(f"\n=== {r['city']} ({r['iata']}) ===")
        # Header row: ret dates (prices per family)
        header = "          " + "  ".join(f"{d[-5:]:<12}" for d in ret_dates)
        lines.append(header)
        lines.append("          " + "-" * (14 * len(ret_dates)))
        if families > 1:
            lines.append(f"  (prices per family — multiply by {families} for group total)")

        for i, dep in enumerate(dep_dates):
            row_cells = []
            for j, ret in enumerate(ret_dates):
                if i < len(r["grid"]) and j < len(r["grid"][i]):
                    cell = r["grid"][i][j]
                    price = cell.get("chosen_cad")
                    if price is None:
                        row_cells.append(f"{'no-data':<12}")
                    else:
                        chosen = cell.get("chosen", "")
                        mark = "*" if chosen == "nonstop" else " "
                        row_cells.append(f"${price:<6}{mark}{'ns' if chosen == 'nonstop' else 'cx':<5}")
                else:  # pragma: no cover
                    row_cells.append(f"{'?':<12}")
            lines.append(f"{dep[-5:]}  " + "  ".join(row_cells))

        if r["best"]:
            b = r["best"]
            mark = "*nonstop*" if b["chosen"] == "nonstop" else "cheapest"
            best_line = f"  Best: {b['dep']} → {b['ret']}  CA${b['chosen_cad']:,}/family  [{mark}]"
            if families > 1:
                group_total = b["chosen_cad"] * families
                best_line += f"  · group ×{families}: CA${group_total:,}"
            lines.append(best_line)
        else:
            lines.append("  Best: no priceable cells found")

    lines.append("\n--- Recommendation ---")
    lines.append(result["recommendation"])
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    # Resolve origin
    origin = _resolve_origin(args.origin)
    if not origin:
        print(f"Error: could not resolve origin '{args.origin}' to an IATA code.",
              file=sys.stderr)
        return 1

    # Resolve destinations
    if args.country:
        try:
            dests = app.top_cities(args.country, args.max_cities)
        except Exception as e:
            print(f"error: could not expand country '{args.country}': {e}", file=sys.stderr)
            return 1
        if not dests:
            print(f"Error: no cities found for country '{args.country}'.", file=sys.stderr)
            return 1
    else:
        # --city: resolve single city
        raw_city = args.city
        if raw_city.isalpha() and raw_city.isupper() and len(raw_city) == 3:
            iata = raw_city
            city_name = raw_city
        else:
            iata = app.resolve_airport(raw_city)
            city_name = raw_city
        if not iata:
            print(f"Error: could not resolve destination city '{raw_city}'.", file=sys.stderr)
            return 1
        dests = [{"city": city_name, "iata": iata}]

    dep_dates = app.date_range(args.dep_start, args.dep_span)
    ret_dates = app.date_range(args.ret_start, args.ret_span)

    if not dep_dates:
        print(
            f"Error: departure date window is empty or '{args.dep_start}' is not a valid ISO date.",
            file=sys.stderr,
        )
        return 1
    if not ret_dates:
        print(
            f"Error: return date window is empty or '{args.ret_start}' is not a valid ISO date.",
            file=sys.stderr,
        )
        return 1

    result = app.run_search(
        origin=origin,
        dests=dests,
        adults=args.adults,
        child_ages=args.children,
        dep_dates=dep_dates,
        ret_dates=ret_dates,
        threshold_pct=args.nonstop_threshold,
        families=args.families,
    )

    print(_render(result))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
