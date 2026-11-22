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

Score-submission events are the versioned four-topic events emitted by
contracts/scoregate-score/src/events.rs (`score_submitted`):
  (symbol "score", EVENT_VERSION, wallet, asset_pair)
The adapter checks the event name and version before reading the wallet and asset
pair, and skips any other event or any version it does not know. When the contract
changes that layout it must bump EVENT_VERSION, so an unsupported version is skipped
and counted rather than being read with the wrong topic positions.
"""

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

DEFAULT_RPC_URL = "https://soroban-testnet.stellar.org/"

# Topic layout of `score_submitted`: (name, version, wallet, asset_pair).
SCORE_EVENT_NAME = "score"
SUPPORTED_SCORE_EVENT_VERSIONS = (1,)
SCORE_EVENT_TOPIC_COUNT = 4

# Why an event produced no replay record.
SKIP_NOT_SCORE_EVENT = "not a score-submission event"
SKIP_UNSUPPORTED_VERSION = "unsupported score event version"
SKIP_MALFORMED = "malformed score event"


def fetch_events(rpc_url, contract_id, start_ledger, limit=100):
    events = []
    cursor = None

    headers = {
        "Content-Type": "application/json",
        "User-Agent": "ScoreGate-Replay-PoC/1.0"
    }

    while True:
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
            with urllib.request.urlopen(req) as response:
                res = json.loads(response.read().decode("utf-8"))
        except urllib.error.URLError as e:
            print(f"RPC Error: {e}", file=sys.stderr)
            sys.exit(1)

        if "error" in res:
            print(f"RPC Response Error: {res['error']}", file=sys.stderr)
            sys.exit(1)

        result = res.get("result", {})
        page_events = result.get("events", [])
        if not page_events:
            break

        events.extend(page_events)

        # Check cursor for pagination
        latest_cursor = result.get("cursor")
        if not latest_cursor or latest_cursor == cursor or len(page_events) < limit:
            break
        cursor = latest_cursor
        time.sleep(0.1)  # Small delay to respect rate limits

    return events


def extract_string(topic_val):
    """Return the string value of a topic element regardless of encoding."""
    if isinstance(topic_val, str):
        return topic_val
    if isinstance(topic_val, dict):
        # Soroban RPC may wrap values as {"type": "...", "value": "..."}
        return topic_val.get("value") or topic_val.get("str") or ""
    return ""


def extract_version(topic_val):
    """Return the event schema version in a topic element as an int, or None.

    The version is a u32, which the RPC JSON carries as a number, a decimal
    string, or a dict such as {"u32": 1} or {"type": "u32", "value": 1}.
    """
    if isinstance(topic_val, dict):
        topic_val = topic_val.get("u32", topic_val.get("value"))
    if isinstance(topic_val, bool):
        return None
    if isinstance(topic_val, int):
        return topic_val
    if isinstance(topic_val, str) and topic_val.isdigit():
        return int(topic_val)
    return None


def decode_score_event(event):
    """Decode a raw Soroban RPC event into (SnapshotEntry dict, None) or (None, reason).

    ScoreGate score-submission events (`score_submitted` in
    contracts/scoregate-score/src/events.rs) have four topics:
      topic[0]: event name symbol, 'score'
      topic[1]: schema version (EVENT_VERSION, a u32)
      topic[2]: wallet address (Stellar G.../C... address string)
      topic[3]: asset pair symbol (e.g. 'XLM_USDC')

    The name and version are checked before the wallet and asset pair are read,
    so an unrelated event with the same number of topics, or a version whose
    layout this adapter does not know, yields a skip reason instead of a record.

    In the Soroban RPC JSON response, topics are represented as XDR-value
    JSON objects.  Each topic element may be either a plain string (for Symbol
    and Address types the Stellar SDK serialises as strings) or a dict with a
    type key.  This adapter handles both representations so it works across
    different RPC serialisation versions.
    """
    topics = event.get("topic") or []
    if not topics or extract_string(topics[0]) != SCORE_EVENT_NAME:
        return None, SKIP_NOT_SCORE_EVENT

    if len(topics) < 2 or extract_version(topics[1]) not in SUPPORTED_SCORE_EVENT_VERSIONS:
        return None, SKIP_UNSUPPORTED_VERSION

    if len(topics) != SCORE_EVENT_TOPIC_COUNT:
        return None, SKIP_MALFORMED

    wallet = extract_string(topics[2])
    asset_pair = extract_string(topics[3])
    if not wallet or not asset_pair:
        return None, SKIP_MALFORMED

    # trades is None: score events don't carry raw trade data; tools/replay
    # derives its score from the trades field (falling back to default_score
    # when None), so omitting it is correct for snapshot replay purposes.
    return {
        "wallet": wallet,
        "asset_pair": asset_pair,
        "trades": None,
    }, None


def convert_event_to_snapshot_entry(event):
    """Convert a raw Soroban RPC event to a replay-compatible SnapshotEntry.

    Returns a SnapshotEntry-compatible dict (wallet, asset_pair, trades) that
    tools/replay can deserialise directly, or None if the event is not a
    supported score-submission event (see decode_score_event).
    """
    entry, _ = decode_score_event(event)
    return entry


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

    args = parser.parse_args()

    print(f"Fetching events from {args.rpc_url} starting at ledger {args.start_ledger}...")
    raw_events = fetch_events(args.rpc_url, args.contract_id, args.start_ledger)
    print(f"Fetched {len(raw_events)} events.")

    count = 0
    skipped = {}
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
                ndjson_entry, reason = decode_score_event(item)
                if ndjson_entry is None:
                    skipped[reason] = skipped.get(reason, 0) + 1
                    continue

            f.write(json.dumps(ndjson_entry) + "\n")
            count += 1

    fmt_note = f" (format={args.format}"
    if args.format == "replay" and skipped:
        details = ", ".join(f"{n} {reason}" for reason, n in sorted(skipped.items()))
        fmt_note += f", {sum(skipped.values())} events skipped: {details}"
    fmt_note += ")"
    print(f"Successfully exported {count} snapshot records to {args.output}{fmt_note}")


if __name__ == "__main__":
    main()
