import os
import json
import time
import base64
import datetime
import random
import itertools
import sys

import requests
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("GITHUB_TOKEN")
REPO = os.getenv("GITHUB_REPO")
INTERVAL = int(os.getenv("UPDATE_INTERVAL", 10))
INTEGRATION_TIME_S = float(os.getenv("INTEGRATION_TIME", 5))
COINCIDENCE_WINDOW_PS = int(os.getenv("COINCIDENCE_WINDOW_PS", 1000))

if not TOKEN or not REPO:
    sys.exit("GITHUB_TOKEN and GITHUB_REPO must be set in .env")

API_BASE = f"https://api.github.com/repos/{REPO}/contents/data.json"
HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}

try:
    from TimeTagger import (
        createTimeTagger,
        ChannelEdge,
        Countrate,
        Coincidences,
        CoincidenceTimestamp,
    )
    HARDWARE = True
except ImportError:
    HARDWARE = False
    print("TimeTagger SDK not found — running in dummy mode.")


class Hardware:
    def __init__(self):
        self.tagger = createTimeTagger()
        self.channels = self.tagger.getChannelList(ChannelEdge.Rising)
        if not self.channels:
            raise RuntimeError("No rising-edge channels detected on the Time Tagger.")
        self.pairs = list(itertools.combinations(self.channels, 2))
        self.coinc = Coincidences(
            self.tagger,
            [list(p) for p in self.pairs],
            COINCIDENCE_WINDOW_PS,
            CoincidenceTimestamp.Last,
        )
        self.virtual_channels = self.coinc.getChannels()
        self.singles = Countrate(self.tagger, self.channels)
        self.coinc_rate = Countrate(self.tagger, self.virtual_channels)

    def read(self):
        integration_ps = int(INTEGRATION_TIME_S * 1e12)
        for m in (self.singles, self.coinc_rate):
            m.clear()
            m.startFor(integration_ps)
        self.singles.waitUntilFinished()
        self.coinc_rate.waitUntilFinished()

        single_rates = self.singles.getData()
        coinc_rates = self.coinc_rate.getData()

        channels = {f"ch{ch}": float(r) for ch, r in zip(self.channels, single_rates)}
        coincidences = {
            f"coincidences_ch{a}_ch{b}": float(r)
            for (a, b), r in zip(self.pairs, coinc_rates)
        }
        return channels, coincidences


def dummy_read():
    channels = {f"ch{i}": random.uniform(1e4, 1e5) for i in range(1, 5)}
    coincidences = {}
    keys = list(channels.keys())
    for a, b in itertools.combinations(keys, 2):
        coincidences[f"coincidences_{a}_{b}"] = random.uniform(10, 500)
    return channels, coincidences


def push_data(channels, coincidences):
    payload = {
        "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
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


def main():
    reader = Hardware() if HARDWARE else None
    mode = "hardware" if HARDWARE else "dummy"
    print(f"Publisher started in {mode} mode — pushing every {INTERVAL}s to {REPO}")
    while True:
        try:
            channels, coincidences = reader.read() if reader else dummy_read()
            push_data(channels, coincidences)
            print(f"[{datetime.datetime.utcnow().isoformat()}Z] pushed "
                  f"{len(channels)} channels, {len(coincidences)} coincidences")
        except Exception as e:
            print(f"[warn] loop error: {e}")
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
