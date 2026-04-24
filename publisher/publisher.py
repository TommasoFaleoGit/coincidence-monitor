import os
import json
import time
import base64
from datetime import datetime, timezone
import random
import itertools
import sys

import requests
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("GITHUB_TOKEN")
REPO = os.getenv("GITHUB_REPO")
INTERVAL = int(os.getenv("UPDATE_INTERVAL", 10))

SQ_IP = os.getenv("SQ_IP", "192.168.1.1")
SQ_CONTROL_PORT = int(os.getenv("SQ_CONTROL_PORT", 12000))
SQ_COUNTS_PORT = int(os.getenv("SQ_COUNTS_PORT", 12345))

ENABLE_COINCIDENCES = os.getenv("ENABLE_COINCIDENCES", "false").strip().lower() == "true"
COINCIDENCE_WINDOW_PS = int(os.getenv("COINCIDENCE_WINDOW_PS", 1000))

if not TOKEN or not REPO:
    sys.exit("GITHUB_TOKEN and GITHUB_REPO must be set in .env")

API_BASE = f"https://api.github.com/repos/{REPO}/contents/data.json"
HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}


class SQReader:
    """Read-only consumer of the Single Quantum WebSQ count stream.

    Uses only non-mutating SDK calls: connect, get_number_of_detectors,
    get_measurement_periode, acquire_cnts, close. Never enables detectors,
    sets bias/trigger/period, or runs calibration — a running lab
    measurement must remain undisturbed.
    """

    def __init__(self):
        from WebSQControl import WebSQControl
        self.websq = WebSQControl(
            TCP_IP_ADR=SQ_IP,
            CONTROL_PORT=SQ_CONTROL_PORT,
            COUNTS_PORT=SQ_COUNTS_PORT,
        )
        self.websq.connect()
        self.n_detectors = self.websq.get_number_of_detectors()
        self.period_ms = float(self.websq.get_measurement_periode())
        if self.period_ms <= 0:
            raise RuntimeError(f"Invalid SQ measurement period: {self.period_ms} ms")
        print(f"SQ connected at {SQ_IP}: {self.n_detectors} detectors, period {self.period_ms} ms")

    def read(self):
        samples = self.websq.acquire_cnts(1)
        if not samples:
            return None
        row = samples[-1]
        counts = row[1:1 + self.n_detectors]
        rate = 1000.0 / self.period_ms
        return {f"ch{i + 1}": float(c) * rate for i, c in enumerate(counts)}

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
        print(f"Swabian coincidences: {len(self.pairs)} pairs, window {COINCIDENCE_WINDOW_PS} ps")

    def read(self):
        rates = self.rate.getData()
        return {
            f"coincidences_ch{a}_ch{b}": float(r)
            for (a, b), r in zip(self.pairs, rates)
        }


def dummy_singles():
    return {f"ch{i}": random.uniform(1e4, 1e5) for i in range(1, 5)}


def push_data(channels, coincidences):
    payload = {
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "channels": channels,
        "coincidences": coincidences,
    }
    body = json.dumps(payload, indent=2).encode("utf-8")

    try:
        r = requests.get(API_BASE, headers=HEADERS, timeout=15)
        r.raise_for_status()
        sha = r.json()["sha"]
    except requests.RequestException as e:
        print(f"[warn] GET failed: {e}")
        return

    put_body = {
        "message": "update counts",
        "content": base64.b64encode(body).decode("ascii"),
        "sha": sha,
    }
    try:
        r = requests.put(API_BASE, headers=HEADERS, json=put_body, timeout=15)
        r.raise_for_status()
    except requests.RequestException as e:
        print(f"[warn] PUT failed: {e} — {getattr(e.response, 'text', '')}")


def build_sq_reader():
    try:
        return SQReader()
    except Exception as e:
        print(f"[warn] SQ init failed ({e}) — falling back to dummy singles.")
        return None


def build_coinc_reader():
    if not ENABLE_COINCIDENCES:
        return None
    try:
        return SwabianCoincidenceReader()
    except Exception as e:
        print(f"[warn] Swabian coincidences disabled ({e}).")
        return None


def main():
    sq = build_sq_reader()
    coinc = build_coinc_reader()
    mode = "SQ" if sq else "dummy"
    if coinc:
        mode += "+Swabian"
    print(f"Publisher started in {mode} mode — pushing every {INTERVAL}s to {REPO}")

    try:
        while True:
            try:
                channels = sq.read() if sq else dummy_singles()
                if channels is None:
                    print("[warn] no SQ sample available yet, skipping push")
                else:
                    coincidences = coinc.read() if coinc else {}
                    push_data(channels, coincidences)
                    print(f"[{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ')}] pushed "
                          f"{len(channels)} channels, {len(coincidences)} coincidences")
            except Exception as e:
                print(f"[warn] loop error: {e}")
            time.sleep(INTERVAL)
    finally:
        if sq:
            sq.close()


if __name__ == "__main__":
    main()
