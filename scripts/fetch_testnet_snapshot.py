#!/usr/bin/env python3
"""
Proof-of-Concept: Soroban RPC Event Exporter for Replay & Forensics

Fetches contract events/transactions directly from a Soroban RPC endpoint
(e.g., testnet) and outputs them as a frozen NDJSON snapshot for tools/replay.

Output formats (--format):
  raw     -- Raw RPC event records (event_id, ledger, contract_id, topic, value, …)
  replay  -- Replay-compatible SnapshotEntry records (wallet, asset_pair, trades)
             that can be ingested directly by tools/replay without JSON
             deserialization errors. This is the default format.

The --format replay adapter resolves the incompatibility described in issue #275:
fetch_testnet_snapshot.py previously emitted raw RPC records, but tools/replay
expects SnapshotEntry { wallet, asset_pair, trades }. The adapter parses Soroban
event topics to extract those fields from every score-submission event.

Bounded exports (issue #350):
  Every RPC request has a configurable deadline (--request-timeout) so a stalled
  endpoint terminates with an actionable error instead of hanging forever.
  Pagination is bounded by configurable page, record, and byte ceilings
  (--max-pages, --max-records, --max-bytes). When any ceiling is hit the export
  is reported as TRUNCATED (non-zero exit) so partial data is never presented as
  a complete, successful export.
"""

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

DEFAULT_RPC_URL = "https://soroban-testnet.stellar.org/"

# Default ceilings keep a runaway ledger range from exhausting local memory.
DEFAULT_REQUEST_TIMEOUT = 30.0
DEFAULT_MAX_PAGES = 1000
DEFAULT_MAX_RECORDS = 100000
DEFAULT_MAX_BYTES = 256 * 1024 * 1024


class ExportTruncated(Exception):
    """Raised when a configured page/record/byte ceiling is reached."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def fetch_events(rpc_url, contract_id, start_ledger, limit=100,
                 request_timeout=DEFAULT_REQUEST_TIMEOUT,
                 max_pages=DEFAULT_MAX_PAGES,
                 max_records=DEFAULT_MAX_RECORDS,
                 max_bytes=DEFAULT_MAX_BYTES):
    """Fetch events with bounded request deadlines and collection ceilings.

    Returns (events, truncated_reason). ``truncated_reason`` is None for a
    complete export, or a human-readable string when a ceiling was hit.
    """
    events = []
    cursor = None
    pages = 0
    total_bytes = 0

    headers = {
        "Content-Type": "application/json",
        "User-Agent": "ScoreGate-Replay-PoC/1.0"
    }

    while True:
        if pages >= max_pages:
            return events, f"page ceiling reached (--max-pages={max_pages})"

        params = {
            "filters": [{"type": "contract"}]
        }
        if contract_id:
            params["filters"][0]["contractIds"] = [contract_id]

        if cursor:
            params["pagination"] = {"cursor": cursor, "limit": limit}
        else:
            params["startLedger"] = start_ledger
            params["pagination"] = {"limit": limit}

        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getEvents",
            "params": params
        }

        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(rpc_url, data=data, headers=headers)

        try:
            with urllib.request.urlopen(req, timeout=request_timeout) as response:
                body = response.read()
        except urllib.error.URLError as e:
            print(
                f"RPC Error: {e} (request timed out after {request_timeout}s; "
                f"raise --request-timeout or check the endpoint)",
                file=sys.stderr,
            )
            sys.exit(1)
        except TimeoutError:
            print(
                f"RPC Error: request timed out after {request_timeout}s; "
                f"raise --request-timeout or check the endpoint",
                file=sys.stderr,
            )
            sys.exit(1)

        total_bytes += len(body)
        if total_bytes > max_bytes:
            return events, f"byte ceiling reached (--max-bytes={max_bytes})"

        try:
            res = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as e:
            print(f"RPC Error: malformed response: {e}", file=sys.stderr)
            sys.exit(1)

        if "error" in res:
            print(f"RPC Response Error: {res['error']}", file=sys.stderr)
            sys.exit(1)

        result = res.get("result", {})
        page_events = result.get("events", [])
        if not page_events:
            break

        pages += 1
        events.extend(page_events)

        if len(events) > max_records:
            return events, f"record ceiling reached (--max-records={max_records})"

        # Check cursor for pagination
        latest_cursor = result.get("cursor")
        if not latest_cursor or latest_cursor == cursor or len(page_events) < limit:
            break
        cursor = latest_cursor
        time.sleep(0.1)  # Small delay to respect rate limits

    return events, None


def convert_event_to_snapshot_entry(event):
    """Convert a raw Soroban RPC event to a replay-compatible SnapshotEntry.

    ScoreGate score-submission events ('scr_sub') have topics in the format:
      topic[0]: event name symbol (e.g. 'scr_sub')
      topic[1]: wallet address (Stellar G.../C... address string)
      topic[2]: asset pair symbol (e.g. 'XLM_USDC')

    In the Soroban RPC JSON response, topics are represented as XDR-value
    JSON objects.  Each topic element may be either a plain string (for Symbol
    and Address types the Stellar SDK serialises as strings) or a dict with a
    type key.  This adapter handles both representations so it works across
    different RPC serialisation versions.

    Returns a SnapshotEntry-compatible dict (wallet, asset_pair, trades) that
    tools/replay can deserialise directly, or None if the event does not carry
    the expected score-submission topic structure.
    """
    topics = event.get("topic", [])
    if not topics or len(topics) < 3:
        return None

    def extract_string(topic_val):
        """Return the string value of a topic element regardless of encoding."""
        if isinstance(topic_val, str):
            return topic_val
        if isinstance(topic_val, dict):
            # Soroban RPC may wrap values as {"type": "...", "value": "..."}
            return topic_val.get("value") or topic_val.get("str") or ""
        return ""

    wallet = extract_string(topics[1])
    asset_pair = extract_string(topics[2])

    if not wallet or not asset_pair:
        return None

    # trades is None: score events don't carry raw trade data; tools/replay
    # derives its score from the trades field (falling back to default_score
    # when None), so omitting it is correct for snapshot replay purposes.
    return {
        "wallet": wallet,
        "asset_pair": asset_pair,
        "trades": None,
    }


def main():
    parser = argparse.ArgumentParser(
        description="Export Soroban RPC events to NDJSON snapshot format."
    )
    parser.add_argument("--rpc-url", default=DEFAULT_RPC_URL,
                        help="Soroban RPC endpoint URL")
    parser.add_argument("--contract-id",
                        help="Target Soroban contract ID (optional)")
    parser.add_argument("--start-ledger", type=int, default=4324800,
                        help="Start ledger number")
    parser.add_argument("--output", default="testnet_snapshot.ndjson",
                        help="Output NDJSON file path")
    parser.add_argument(
        "--format",
        choices=["raw", "replay"],
        default="replay",
        help=(
            "Output format: 'raw' emits raw RPC event records; "
            "'replay' (default) converts events to SnapshotEntry format "
            "that tools/replay can ingest without deserialization errors."
        ),
    )
    parser.add_argument(
        "--request-timeout", type=float, default=DEFAULT_REQUEST_TIMEOUT,
        help="Per-request deadline in seconds (default: %(default)s)",
    )
    parser.add_argument(
        "--max-pages", type=int, default=DEFAULT_MAX_PAGES,
        help="Maximum number of RPC pages to fetch (default: %(default)s)",
    )
    parser.add_argument(
        "--max-records", type=int, default=DEFAULT_MAX_RECORDS,
        help="Maximum number of events to collect (default: %(default)s)",
    )
    parser.add_argument(
        "--max-bytes", type=int, default=DEFAULT_MAX_BYTES,
        help="Maximum total response bytes to read (default: %(default)s)",
    )

    args = parser.parse_args()

    print(f"Fetching events from {args.rpc_url} starting at ledger {args.start_ledger}...")
    raw_events, truncated_reason = fetch_events(
        args.rpc_url,
        args.contract_id,
        args.start_ledger,
        request_timeout=args.request_timeout,
        max_pages=args.max_pages,
        max_records=args.max_records,
        max_bytes=args.max_bytes,
    )
    print(f"Fetched {len(raw_events)} events.")

    count = 0
    skipped = 0
    with open(args.output, "w") as f:
        for item in raw_events:
            if args.format == "raw":
                ndjson_entry = {
                    "event_id": item.get("id"),
                    "ledger": item.get("ledger"),
                    "ledger_closed_at": item.get("ledgerClosedAt"),
                    "contract_id": item.get("contractId"),
                    "tx_hash": item.get("txHash"),
                    "topic": item.get("topic"),
                    "value": item.get("value"),
                    "in_successful_contract_call": item.get("inSuccessfulContractCall"),
                }
            else:
                # replay format: convert to SnapshotEntry
                ndjson_entry = convert_event_to_snapshot_entry(item)
                if ndjson_entry is None:
                    skipped += 1
                    continue

            f.write(json.dumps(ndjson_entry) + "\n")
            count += 1

    fmt_note = f" (format={args.format}"
    if args.format == "replay" and skipped:
        fmt_note += f", {skipped} non-score events skipped"
    fmt_note += ")"

    if truncated_reason is not None:
        print(
            f"TRUNCATED: exported {count} snapshot records to {args.output}{fmt_note} "
            f"-- {truncated_reason}. Output is INCOMPLETE; raise the relevant "
            f"limit or narrow the ledger range to obtain a full export.",
            file=sys.stderr,
        )
        sys.exit(2)

    print(f"Successfully exported {count} snapshot records to {args.output}{fmt_note}")


if __name__ == "__main__":
    main()
