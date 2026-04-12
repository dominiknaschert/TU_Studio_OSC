# OSC Spatial Sender

A simple GUI tool using a network of MIDI-mappable LFOs and assignable sliders to send spatial audio positions via OSC to the Seamless plugin suite.

Built for the TU Berlin WFS studio.

---

## features

- control **X / Y / Z** position + **Ambi** and **WFS gain** independently per source
- send to **Ambisonics (renderer 0)** and **WFS (renderer 1)** simultaneously
- **8 sine LFOs** — assign to any parameter of any source
- **live XY view** — see all sources move in real time
- **dynamic source count** (1–16)

---

## install

```bash
git clone https://github.com/yourname/osc-spatial-sender
cd osc-spatial-sender

python -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate

pip install -r requirements.txt
```

---

## run

```bash
python osc_sender.py
```

---

## osc protocol

| path | format | notes |
|---|---|---|
| `/source/pos/xyz` | `ifff` | source_index, x, y, z — values between -1 and 1 |
| `/send/gain` | `iif` | source_index, renderer_index, gain — renderer 0=ambi, 1=wfs |

default target: `riviera.ak.tu-berlin.de:4455`

---

## requirements

- Python 3.10+
- `python-osc`
- `customtkinter`
- `matplotlib`

---

## license

MIT — do whatever you want with it.
