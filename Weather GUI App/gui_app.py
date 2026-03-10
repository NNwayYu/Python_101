import threading
import queue
from datetime import datetime
from zoneinfo import ZoneInfo
import tkinter as tk
from tkinter import ttk, messagebox

import requests
import ollama
from ollama import ResponseError

# ----------------------------
# Dropdown options
# ----------------------------
CITIES = [
    "Singapore",
    "Kuala Lumpur",
    "Johor Bahru",
    "Bangkok",
    "Jakarta",
    "Manila",
    "Hong Kong",
    "Tokyo",
    "Seoul",
    "Sydney",
    "Other..."
]

MODELS = [
    "llama3",
    "mistral"
]

# ----------------------------
# Weather API (Open-Meteo)
# ----------------------------
def geocode_place(place_name: str) -> dict:
    geo_url = "https://geocoding-api.open-meteo.com/v1/search"
    r = requests.get(
        geo_url,
        params={"name": place_name, "count": 1, "language": "en"},
        timeout=20,
    )
    r.raise_for_status()
    geo = r.json()
    if not geo.get("results"):
        raise RuntimeError(f"Could not find '{place_name}' in geocoding results.")
    return geo["results"][0]

def fetch_current_weather(lat: float, lon: float, timezone: str) -> dict:
    wx_url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": lat,
        "longitude": lon,
        "timezone": timezone,
        "current": "temperature_2m,relative_humidity_2m,apparent_temperature,precipitation,weather_code,wind_speed_10m",
    }
    r = requests.get(wx_url, params=params, timeout=20)
    r.raise_for_status()
    wx = r.json()
    if "current" not in wx:
        raise RuntimeError(f"Unexpected weather response: {wx}")
    return wx["current"]

def build_prompt(place_label: str, fetched_at: str, timezone: str, current_json: dict) -> str:
    return f"""
You are given LIVE weather data from an API. Summarize it for a normal person in 3–6 bullet points.
Be specific: temperature, feels-like, humidity, wind, precipitation.
If something is missing, say so briefly.

Location: {place_label}
Fetched at: {fetched_at} ({timezone})

Current data (raw JSON):
{current_json}
""".strip()

# ----------------------------
# Tkinter App
# ----------------------------
class WeatherAIApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Real-time Weather")
        self.geometry("860x560")

        self.ui_queue = queue.Queue()
        self.worker_thread = None
        self.stop_flag = threading.Event()

        self._build_ui()
        self.after(50, self._drain_queue)

    def _build_ui(self):
        top = ttk.Frame(self, padding=10)
        top.pack(fill="x")

        # City dropdown + optional custom entry
        ttk.Label(top, text="City:").grid(row=0, column=0, sticky="w")
        self.city_var = tk.StringVar(value=CITIES[0])
        self.city_combo = ttk.Combobox(
            top, textvariable=self.city_var, values=CITIES, state="readonly", width=22
        )
        self.city_combo.grid(row=0, column=1, padx=(6, 14), sticky="w")
        self.city_combo.bind("<<ComboboxSelected>>", self._on_city_selected)

        self.city_custom_var = tk.StringVar(value="")
        self.city_custom_entry = ttk.Entry(
            top, textvariable=self.city_custom_var, width=22, state="disabled"
        )
        self.city_custom_entry.grid(row=0, column=2, padx=(0, 14), sticky="w")
        ttk.Label(top, text="(Custom city when 'Other...')").grid(row=0, column=3, sticky="w")

        # Model dropdown (restricted)
        ttk.Label(top, text="Model:").grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.model_var = tk.StringVar(value=MODELS[0])
        self.model_combo = ttk.Combobox(
            top, textvariable=self.model_var, values=MODELS, state="readonly", width=22
        )
        self.model_combo.grid(row=1, column=1, padx=(6, 14), sticky="w", pady=(8, 0))

        # Buttons
        btns = ttk.Frame(top)
        btns.grid(row=0, column=4, rowspan=2, padx=(18, 0), sticky="ns")

        self.btn_run = ttk.Button(btns, text="Get Live Weather + Explain", command=self.on_run)
        self.btn_run.pack(fill="x")

        self.btn_stop = ttk.Button(btns, text="Stop", command=self.on_stop, state="disabled")
        self.btn_stop.pack(fill="x", pady=(8, 0))

        # Output area
        mid = ttk.Frame(self, padding=(10, 0, 10, 10))
        mid.pack(fill="both", expand=True)

        self.status_var = tk.StringVar(value="Ready.")
        ttk.Label(mid, textvariable=self.status_var).pack(anchor="w")

        self.text = tk.Text(mid, wrap="word")
        self.text.pack(fill="both", expand=True, pady=(6, 0))
        

        try:
            self.text.configure(font=("Segoe UI", 10))
        except Exception:
            pass

    def _on_city_selected(self, _evt=None):
        if self.city_var.get() == "Other...":
            self.city_custom_entry.configure(state="normal")
            self.city_custom_entry.focus_set()
        else:
            self.city_custom_entry.configure(state="disabled")
            self.city_custom_var.set("")

    def _resolve_city(self) -> str:
        city = self.city_var.get().strip()
        if city == "Other...":
            city = self.city_custom_var.get().strip()
        return city or "Singapore"

    def set_status(self, s: str):
        self.status_var.set(s)

    def append_text(self, s: str):
        self.text.insert("end", s)
        self.text.see("end")

    def clear_text(self):
        self.text.delete("1.0", "end")

    def on_run(self):
        if self.worker_thread and self.worker_thread.is_alive():
            messagebox.showinfo("Busy", "A request is already running. Click Stop first.")
            return

        city = self._resolve_city()
        model = self.model_var.get()

        self.stop_flag.clear()
        self.btn_run.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self.clear_text()
        self.set_status(f"Fetching live weather for {city}...")

        self.worker_thread = threading.Thread(
            target=self.worker_job,
            args=(city, model),
            daemon=True
        )
        self.worker_thread.start()

    def on_stop(self):
        self.stop_flag.set()
        self.set_status("Stopping (will stop after current chunk/network call)...")

    def worker_job(self, city: str, model: str):
        try:
            loc = geocode_place(city)
            lat, lon = loc["latitude"], loc["longitude"]
            place_label = city if city.lower() == "singapore" else f'{loc.get("name", city)}, {loc.get("country", "")}'

            timezone = loc.get("timezone", "UTC")

            current = fetch_current_weather(lat, lon, timezone)
            fetched_at = datetime.now(ZoneInfo(timezone)).strftime("%A, %d %b %Y, %I:%M %p")


            self.ui_queue.put(("append", f"Location: {place_label}\n"))
            self.ui_queue.put(("append", f"Fetched:  {fetched_at} ({timezone})\n\n"))
            self.ui_queue.put(("append", "Weather summary:\n"))

            prompt = build_prompt(place_label, fetched_at, timezone, current)
            self.ui_queue.put(("status", f"Streaming from Ollama model: {model} ..."))

            stream = ollama.chat(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                stream=True,
            )

            for chunk in stream:
                if self.stop_flag.is_set():
                    break
                msg = chunk.get("message") or {}
                text = msg.get("content") or ""
                if text:
                    self.ui_queue.put(("append", text))

            self.ui_queue.put(("append", "\n"))
            self.ui_queue.put(("status", "Done." if not self.stop_flag.is_set() else "Stopped."))

        except ResponseError as e:
            self.ui_queue.put(("status", "Error."))
            self.ui_queue.put(("error",
                f"Ollama error ({e.status_code}): {e}\n\n"
                f"Ensure Ollama is running and model '{model}' is pulled."
            ))
        except Exception as e:
            self.ui_queue.put(("status", "Error."))
            self.ui_queue.put(("error", str(e)))
        finally:
            self.ui_queue.put(("ui_done", None))

    def _drain_queue(self):
        try:
            while True:
                kind, payload = self.ui_queue.get_nowait()
                if kind == "status":
                    self.set_status(payload)
                elif kind == "append":
                    self.append_text(payload)
                elif kind == "error":
                    messagebox.showerror("Error", payload)
                elif kind == "ui_done":
                    self.btn_run.configure(state="normal")
                    self.btn_stop.configure(state="disabled")
        except queue.Empty:
            pass

        self.after(50, self._drain_queue)

if __name__ == "__main__":
    app = WeatherAIApp()
    app.mainloop()
