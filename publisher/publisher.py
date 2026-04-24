import os
import json
import time
import base64
import random
import itertools
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

INTERVAL = int(os.getenv("UPDATE_INTERVAL", 10))

ENABLE_COINCIDENCES = os.getenv("ENABLE_COINCIDENCES", "false").strip().lower() == "true"
COINCIDENCE_WINDOW_PS = int(os.getenv("COINCIDENCE_WINDOW_PS", 1000))

DRIVERS_JSON = Path(__file__).parent / "drivers.json"


def _target(token_var, repo_var):
    token = os.getenv(token_var, "").strip()
    repo = os.getenv(repo_var, "").strip()
    if not token or not repo:
        return None
    return {
        "label": repo,
        "api_url": f"https://api.github.com/repos/{repo}/contents/data.json",
        "headers": {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    }


TARGETS = [t for t in (_target("GITHUB_TOKEN", "GITHUB_REPO"),
                       _target("GITHUB_TOKEN_2", "GITHUB_REPO_2")) if t]

if not TARGETS:
    sys.exit("At least GITHUB_TOKEN and GITHUB_REPO must be set in .env")


def load_driver_configs():
    """Load and validate drivers.json. Returns list of driver dicts with int channel_map."""
    if not DRIVERS_JSON.exists():
        sys.exit(f"Missing {DRIVERS_JSON}. Copy drivers.example.json and edit it.")
    with open(DRIVERS_JSON, "r") as f:
        raw = json.load(f)
    drivers = raw.get("drivers", [])
    if not drivers:
        sys.exit("drivers.json contains no drivers.")

    seen_tt_ids = {}
    normalized = []
    for idx, d in enumerate(drivers):
        for key in ("ip", "control_port", "counts_port", "channel_map"):
            if key not in d:
                sys.exit(f"drivers.json entry #{idx} missing field '{key}'")
        cm = {int(k): int(v) for k, v in d["channel_map"].items()}
        expected_keys = set(range(1, 9))
        if set(cm.keys()) != expected_keys:
            sys.exit(f"drivers.json entry #{idx} ({d['ip']}): channel_map "
                     f"must have exactly keys 1..8, got {sorted(cm.keys())}")
        for sq_ch, tt_id in cm.items():
            if tt_id in seen_tt_ids:
                prev = seen_tt_ids[tt_id]
                sys.exit(f"Duplicate TT channel {tt_id}: "
                         f"{prev} and {d['ip']} ch{sq_ch}")
            seen_tt_ids[tt_id] = f"{d['ip']} ch{sq_ch}"
        normalized.append({
            "ip": d["ip"],
            "control_port": int(d["control_port"]),
            "counts_port": int(d["counts_port"]),
            "channel_map": cm,
        })
    return normalized


class SQReader:
    """Read-only consumer of one Single Quantum driver.

    Uses only non-mutating SDK calls: connect, get_number_of_detectors,
    get_measurement_periode, acquire_cnts, close. Never enables detectors,
    sets bias/trigger/period, or runs calibration — a running lab
    measurement must remain undisturbed.
    """

    def __init__(self, cfg):
        from WebSQControl import WebSQControl
        self.ip = cfg["ip"]
        self.channel_map = cfg["channel_map"]
        self.websq = WebSQControl(
            TCP_IP_ADR=cfg["ip"],
            CONTROL_PORT=cfg["control_port"],
            COUNTS_PORT=cfg["counts_port"],
        )
        self.websq.connect()
        self.n_detectors = self.websq.get_number_of_detectors()
        self.period_ms = float(self.websq.get_measurement_periode())
        if self.period_ms <= 0:
            raise RuntimeError(f"Invalid SQ measurement period: {self.period_ms} ms")
        print(f"SQ connected at {self.ip}: {self.n_detectors} detectors, "
              f"period {self.period_ms} ms")

    def read(self):
        samples = self.websq.acquire_cnts(1)
        if not samples:
            return {}
        row = samples[-1]
        counts = row[1:1 + self.n_detectors]
        rate = 1000.0 / self.period_ms
        out = {}
        for sq_ch_1based, c in enumerate(counts, start=1):
            tt_id = self.channel_map.get(sq_ch_1based)
            if tt_id is None:
                continue
            out[f"ch{tt_id}"] = float(c) * rate
        return out

    def close(self):
        try:
            self.websq.close()
        except Exception:
            pass


class SwabianCoincidenceReader:
    """Optional coincidence-only reader on the Swabian Time Tagger.

    Creates virtual coincidence channels over all pairs of rising-edge
    inputs and measures their rates. Does not reconfigure physical inputs.
    Singles still come from SQ — this class never reports singles.
    """

    def __init__(self):
        from TimeTagger import (
            createTimeTagger,
            ChannelEdge,
            Countrate,
            Coincidences,
            CoincidenceTimestamp,
        )
        self.tagger = createTimeTagger()
        self.channels = self.tagger.getChannelList(ChannelEdge.Rising)
        if len(self.channels) < 2:
            raise RuntimeError("Need at least 2 rising-edge channels for coincidences.")
        self.pairs = list(itertools.combinations(self.channels, 2))
        self.coinc = Coincidences(
            self.tagger,
            [list(p) for p in self.pairs],
            COINCIDENCE_WINDOW_PS,
            CoincidenceTimestamp.Last,
        )
        self.rate = Countrate(self.tagger, self.coinc.getChannels())
        print(f"Swabian coincidences: {len(self.pairs)} pairs, "
              f"window {COINCIDENCE_WINDOW_PS} ps")

    def read(self):
        rates = self.rate.getData()
        return {
            f"coincidences_ch{a}_ch{b}": float(r)
            for (a, b), r in zip(self.pairs, rates)
        }


def dummy_singles(all_driver_cfgs):
    """Random data for every TT channel id present in the driver configs."""
    out = {}
    for d in all_driver_cfgs:
        for tt_id in d["channel_map"].values():
            out[f"ch{tt_id}"] = random.uniform(1e4, 1e5)
    return out


def utc_now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def push_data(channels, coincidences):
    payload = {
        "timestamp": utc_now_iso(),
        "channels": channels,
        "coincidences": coincidences,
    }
    body = json.dumps(payload, indent=2).encode("utf-8")
    content_b64 = base64.b64encode(body).decode("ascii")

    for target in TARGETS:
        try:
            r = requests.get(target["api_url"], headers=target["headers"], timeout=15)
            r.raise_for_status()
            sha = r.json()["sha"]
        except requests.RequestException as e:
            print(f"[warn] GET failed [{target['label']}]: {e}")
            continue

        put_body = {
            "message": "update counts",
            "content": content_b64,
            "sha": sha,
        }
        try:
            r = requests.put(target["api_url"], headers=target["headers"],
                             json=put_body, timeout=15)
            r.raise_for_status()
        except requests.RequestException as e:
            print(f"[warn] PUT failed [{target['label']}]: {e} — "
                  f"{getattr(e.response, 'text', '')}")


def build_sq_readers(driver_cfgs):
    readers = []
    for cfg in driver_cfgs:
        try:
            readers.append(SQReader(cfg))
        except Exception as e:
            print(f"[warn] SQ init failed for {cfg['ip']} ({e}).")
    return readers


def build_coinc_reader():
    if not ENABLE_COINCIDENCES:
        return None
    try:
        return SwabianCoincidenceReader()
    except Exception as e:
        print(f"[warn] Swabian coincidences disabled ({e}).")
        return None


def main():
    driver_cfgs = load_driver_configs()
    readers = build_sq_readers(driver_cfgs)
    coinc = build_coinc_reader()

    if readers:
        mode = f"SQ ({len(readers)}/{len(driver_cfgs)} drivers)"
    else:
        mode = "dummy (all drivers offline)"
    if coinc:
        mode += "+Swabian"
    repos = ", ".join(t["label"] for t in TARGETS)
    print(f"Publisher started in {mode} mode — pushing every {INTERVAL}s to {repos}")

    try:
        while True:
            try:
                channels = {}
                if readers:
                    for r in readers:
                        try:
                            channels.update(r.read())
                        except Exception as e:
                            print(f"[warn] read failed for {r.ip}: {e}")
                else:
                    channels = dummy_singles(driver_cfgs)

                if not channels:
                    print("[warn] no channel data this cycle, skipping push")
                else:
                    coincidences = coinc.read() if coinc else {}
                    push_data(channels, coincidences)
                    print(f"[{utc_now_iso()}] pushed "
                          f"{len(channels)} channels, {len(coincidences)} coincidences")
            except Exception as e:
                print(f"[warn] loop error: {e}")
            time.sleep(INTERVAL)
    finally:
        for r in readers:
            r.close()


if __name__ == "__main__":
    main()
