#!/usr/bin/env python3
"""Lightweight Windows screen recorder: GStreamer + Tkinter.

Video: D3D11 screen capture (DXGI by default), x264, MKV or MP4.
Audio: WASAPI2 loopback and mic, optional separate microphone WAV recording.
Requires PyGObject, GStreamer and its Windows plugins; Tkinter is standard Python.
"""

import json
import os
import queue
import re
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst


Gst.init(None)
APP_NAME = "GSRecorder"
APP_VERSION = "1.0"
APP_TITLE = f"{APP_NAME} {APP_VERSION}"
DEFAULT_DEVICE = "System default"
APPDATA_DIR = Path(os.environ.get("APPDATA", str(Path.home())))
SETTINGS_DIR = APPDATA_DIR / APP_NAME
SETTINGS_PATH = SETTINGS_DIR / "settings.json"
LEGACY_SETTINGS_PATH = APPDATA_DIR / "NewGSRecorder" / "settings.json"
DEFAULT_FOLDER = Path.home() / "Videos" / "Recordings"

# All preset dimensions are even, as required by the I420/x264 video path.
# 608p uses the common 1080x608 approximation to 16:9.
RESOLUTION_PRESETS = {
    "144p": (256, 144),
    "240p": (426, 240),
    "360p": (640, 360),
    "480p": (854, 480),
    "540p": (960, 540),
    "576p": (1024, 576),
    "608p": (1080, 608),
    "720p": (1280, 720),
    "960p": (1706, 960),
    "1080p": (1920, 1080),
}
RESOLUTION_CHOICES = ("Native", *RESOLUTION_PRESETS.keys(), "Custom")


def parse_custom_resolution(value):
    """Accept '3840 × 2160', '3840x2160', or '3840 X 2160'."""
    match = re.fullmatch(r"\s*(\d{1,5})\s*[xX×]\s*(\d{1,5})\s*", value)
    if not match:
        raise ValueError("Custom resolution must look like 3840 × 2160 (width × height).")
    width, height = (int(number) for number in match.groups())
    if not (16 <= width <= 8192 and 16 <= height <= 8192):
        raise ValueError("Width and height must each be between 16 and 8192 pixels.")
    if width % 2 or height % 2:
        raise ValueError("Width and height must be even numbers for H.264 (I420).")
    return width, height


def dimensions_for(settings):
    if settings.resolution == "Native":
        return None
    if settings.resolution == "Custom":
        return parse_custom_resolution(settings.custom_resolution)
    try:
        return RESOLUTION_PRESETS[settings.resolution]
    except KeyError:
        raise ValueError("Unknown resolution preset: " + str(settings.resolution)) from None


@dataclass
class Settings:
    folder: str = str(DEFAULT_FOLDER)
    api: str = "dxgi"
    monitor: int = 0
    fps: int = 30
    video_bitrate: int = 8000  # kbps
    preset: str = "ultrafast"
    encoder_threads: int = 2
    resolution: str = "Native"
    custom_resolution: str = ""
    cursor: bool = True
    container: str = "mkv"  # mp4 is optional; MKV remains the default
    audio_mode: str = "system"
    separate_mic: bool = False  # only active with audio_mode="both"
    audio_bitrate: int = 128  # kbps
    system_device: str = ""  # empty means Windows default
    mic_device: str = ""
    system_volume: float = 1.0
    mic_volume: float = 1.0


def load_settings():
    defaults = asdict(Settings())
    try:
        # Existing NewGSRecorder users keep their prior preferences on upgrade.
        path = SETTINGS_PATH if SETTINGS_PATH.is_file() else LEGACY_SETTINGS_PATH
        saved = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(saved, dict):
            for key in defaults:
                if key in saved:
                    defaults[key] = saved[key]
            # Older GUI used a single 1280x720 resolution option.
            if defaults["resolution"] == "1280x720":
                defaults["resolution"] = "720p"
            if defaults["resolution"] not in RESOLUTION_CHOICES:
                defaults["resolution"] = "Native"
            if defaults["container"] not in ("mkv", "mp4"):
                defaults["container"] = "mkv"
        return Settings(**defaults)
    except (OSError, ValueError, TypeError):
        return Settings()


def save_settings(settings):
    try:
        SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
        SETTINGS_PATH.write_text(
            json.dumps(asdict(settings), indent=2), encoding="utf-8"
        )
    except OSError:
        pass  # Recording must not depend on settings persistence.


def find_aac_encoder():
    for name in ("voaacenc", "avenc_aac"):
        if Gst.ElementFactory.find(name):
            return name
    return None


def mic_sidecar_path(video_path):
    """Use the video stem for an unmistakably matching sidecar filename."""
    return video_path.with_name(video_path.stem + "_mic.wav")


def next_output_filename(folder, container="mkv", separate_mic=False):
    """Choose a collision-free filename, including the optional microphone WAV."""
    if container not in ("mkv", "mp4"):
        raise ValueError("Unsupported container format")
    folder = Path(folder).expanduser()
    folder.mkdir(parents=True, exist_ok=True)
    stem = datetime.now().strftime("Recording_%Y-%m-%d_%H-%M-%S")
    number = 1
    while True:
        suffix = f"_{number}" if number > 1 else ""
        path = folder / f"{stem}{suffix}.{container}"
        mic_path = mic_sidecar_path(path)
        if not path.exists() and not (separate_mic and mic_path.exists()):
            return path
        number += 1


def build_pipeline(settings, encoder):
    """Keep the original video/audio paths except where options require changes."""
    scale = ""
    dimensions = dimensions_for(settings)
    if dimensions is not None:
        # No scaling is added for Native: preserve the proven fast path.
        width, height = dimensions
        scale = (
            f"videoscale ! video/x-raw,width={width},height={height},"
            "pixel-aspect-ratio=1/1 ! "
        )

    if settings.container not in ("mkv", "mp4"):
        raise ValueError("Container must be MKV or MP4")

    # mp4mux expects AVC format and access-unit alignment; MKV retains the
    # originally tested h264parse path, without imposing new caps.
    h264_end = (
        "h264parse ! video/x-h264,stream-format=avc,alignment=au ! queue ! mux. "
        if settings.container == "mp4"
        else "h264parse ! queue ! mux. "
    )
    video = (
        f"d3d11screencapturesrc capture-api={settings.api} "
        f"monitor-index={settings.monitor} "
        f"show-cursor={str(settings.cursor).lower()} ! "
        "queue max-size-buffers=3 max-size-bytes=0 max-size-time=0 "
        "leaky=downstream ! "
        "d3d11download ! videoconvert ! "
        f"{scale}"
        "video/x-raw,format=I420 ! "
        "videorate skip-to-first=true ! "
        f"video/x-raw,framerate={settings.fps}/1 ! "
        f"x264enc speed-preset={settings.preset} "
        "tune=zerolatency "
        f"bitrate={settings.video_bitrate} "
        f"key-int-max={settings.fps * 2} "
        f"bframes=0 threads={settings.encoder_threads} ! "
        + h264_end
    )

    muxer = "mp4mux" if settings.container == "mp4" else "matroskamux"
    header = f"{muxer} name=mux ! filesink name=writer sync=false "
    if settings.audio_mode == "none":
        return header + video

    # This normalization and mixed-audio path remain as in the working script.
    normalize = (
        "queue ! audioconvert ! audioresample ! "
        "audio/x-raw,format=S16LE,rate=48000,channels=2 ! "
    )
    # The MP4 muxer needs raw AAC access units. The original MKV path has
    # no extra caps filter so its working negotiation is unchanged.
    aac_caps = (
        "audio/mpeg,mpegversion=4,stream-format=raw ! "
        if settings.container == "mp4" else ""
    )
    encode = (
        "audioconvert ! audio/x-raw,format=S16LE,rate=48000,channels=2 ! "
        f"{encoder} bitrate={settings.audio_bitrate * 1000} ! "
        + aac_caps + "queue ! mux. "
    )
    system_branch = (
        "wasapi2src name=system_src loopback=true ! "
        + normalize
        + f"volume name=system_gain volume={settings.system_volume:.3f} ! "
    )
    mic_branch = (
        "wasapi2src name=mic_src loopback=false ! "
        + normalize
        + f"volume name=mic_gain volume={settings.mic_volume:.3f} ! "
    )
    if settings.audio_mode == "system":
        audio = system_branch + encode
    elif settings.audio_mode == "mic":
        audio = mic_branch + encode
    elif settings.audio_mode == "both" and settings.separate_mic:
        # Sidecar mode: system/game audio alone goes into MKV/MP4, and mic
        # samples go to a separate, uncompressed WAV. One pipeline shares
        # a clock/base-time. audiorate fills initial gaps with silence to
        # preserve alignment in a WAV file that stores no timestamps.
        audio = (
            system_branch + encode
            + mic_branch
            + "audiorate skip-to-first=false ! "
              "audio/x-raw,format=S16LE,rate=48000,channels=2 ! "
              "wavenc ! filesink name=mic_writer sync=false "
        )
    elif settings.audio_mode == "both":
        audio = (
            "audiomixer name=mix ignore-inactive-pads=true ! "
            + encode
            + system_branch + "mix. "
            + mic_branch + "mix. "
        )
    else:
        raise ValueError("Unsupported audio mode")
    return header + video + audio


def structure_property(struct, key, default=None):
    if struct is None:
        return default
    try:
        if struct.has_field(key):
            return struct.get_value(key)
    except (AttributeError, TypeError, ValueError):
        pass
    return default


def enumerate_wasapi_devices():
    """Find Windows WASAPI2 microphone and render-loopback endpoints.

    Gst.Device providers describe endpoints; create_element() yields the actual
    element's device ID, avoiding assumptions about provider metadata keys.
    """
    monitor = Gst.DeviceMonitor.new()
    monitor.add_filter("Audio/Source", None)
    monitor.add_filter("Audio/Sink", None)
    try:
        if not monitor.start():
            raise RuntimeError("No GStreamer audio device provider could start")
        found = {"system": [], "mic": []}
        seen = {"system": set(), "mic": set()}
        for device in monitor.get_devices():
            try:
                props = device.get_properties()
                api = structure_property(props, "device.api", "")
                if api and str(api).lower() != "wasapi2":
                    continue
                klass = device.get_device_class() or ""
                # Depending on GStreamer version, render-loopback devices may
                # be reported as Audio/Source or Audio/Sink.
                loopback = structure_property(props, "wasapi2.device.loopback", None)
                element = device.create_element(None)
                if element is None:
                    continue
                factory = element.get_factory()
                factory_name = factory.get_name() if factory else ""
                if factory_name not in ("wasapi2src", "wasapi2sink"):
                    continue
                endpoint_id = element.get_property("device")
                if not endpoint_id:
                    endpoint_id = structure_property(props, "device.id", "")
                if not endpoint_id:
                    endpoint_id = structure_property(props, "device.strid", "")
                if not endpoint_id:
                    continue
                if loopback is True:
                    category = "system"
                elif loopback is False:
                    category = "mic"
                elif "Sink" in klass or factory_name == "wasapi2sink":
                    category = "system"
                else:
                    category = "mic"
                if endpoint_id in seen[category]:
                    continue
                seen[category].add(endpoint_id)
                found[category].append((device.get_display_name(), str(endpoint_id)))
            except (AttributeError, TypeError, ValueError, RuntimeError):
                continue
        for category in found:
            found[category].sort(key=lambda item: item[0].casefold())
        return found
    finally:
        monitor.stop()


class RecorderWorker(threading.Thread):
    """GStreamer runs independently of Tk; status and commands use queues."""

    def __init__(self, settings, path, updates):
        super().__init__(daemon=True)
        self.settings = settings
        self.path = path
        self.mic_path = (mic_sidecar_path(path) if settings.audio_mode == "both"
                         and settings.separate_mic else None)
        self.updates = updates
        self.commands = queue.Queue()
        self.stop_requested = threading.Event()

    def notify(self, kind, value=None):
        self.updates.put((kind, value))

    def run(self):
        pipeline = None
        finalized = False
        error_text = None
        stop_sent_at = None
        try:
            required = ["d3d11screencapturesrc", "d3d11download", "videoconvert",
                        "videorate", "x264enc", "h264parse",
                        "mp4mux" if self.settings.container == "mp4" else "matroskamux"]
            if self.settings.resolution != "Native":
                required.append("videoscale")
            if self.settings.audio_mode != "none":
                required += ["wasapi2src", "volume", "audioconvert", "audioresample"]
                if self.settings.audio_mode == "both":
                    if self.settings.separate_mic:
                        required += ["audiorate", "wavenc"]
                    else:
                        required.append("audiomixer")
            missing = [name for name in required if not Gst.ElementFactory.find(name)]
            encoder = find_aac_encoder() if self.settings.audio_mode != "none" else None
            if encoder is None and self.settings.audio_mode != "none":
                missing.append("voaacenc / avenc_aac")
            if missing:
                raise RuntimeError("Missing GStreamer elements: " + ", ".join(missing))

            pipeline = Gst.parse_launch(build_pipeline(self.settings, encoder))
            pipeline.get_by_name("writer").set_property("location", str(self.path))
            if self.mic_path is not None:
                pipeline.get_by_name("mic_writer").set_property("location", str(self.mic_path))
            for element_name, endpoint_id in (
                ("system_src", self.settings.system_device),
                ("mic_src", self.settings.mic_device),
            ):
                if endpoint_id:
                    element = pipeline.get_by_name(element_name)
                    if element is not None:
                        element.set_property("device", endpoint_id)

            bus = pipeline.get_bus()
            result = pipeline.set_state(Gst.State.PLAYING)
            if result == Gst.StateChangeReturn.FAILURE:
                raise RuntimeError("Unable to start GStreamer pipeline")
            self.notify("started", str(self.path))

            # Polling the Gst bus in this thread avoids mixing Tk and GLib loops.
            while True:
                while True:
                    try:
                        command = self.commands.get_nowait()
                    except queue.Empty:
                        break
                    name, value = command
                    element = pipeline.get_by_name(name)
                    if element is not None:
                        element.set_property("volume", float(value))

                if self.stop_requested.is_set() and stop_sent_at is None:
                    self.notify("status", "Finalizing recording files...")
                    stop_sent_at = time.monotonic()
                    if not pipeline.send_event(Gst.Event.new_eos()):
                        raise RuntimeError("Could not send EOS to finalize the recording files")

                if stop_sent_at and time.monotonic() - stop_sent_at > 15:
                    raise RuntimeError("Timed out while finalizing recording files (15 seconds)")

                message = bus.timed_pop_filtered(
                    100 * Gst.MSECOND,
                    Gst.MessageType.EOS | Gst.MessageType.ERROR | Gst.MessageType.WARNING,
                )
                if message is None:
                    continue
                if message.type == Gst.MessageType.EOS:
                    finalized = True
                    break
                if message.type == Gst.MessageType.ERROR:
                    err, debug = message.parse_error()
                    raise RuntimeError(f"{err}\n{debug or ''}")
                if message.type == Gst.MessageType.WARNING:
                    warn, debug = message.parse_warning()
                    self.notify("warning", f"{warn}: {debug or ''}")
        except Exception as exc:
            error_text = str(exc)
        finally:
            if pipeline is not None:
                pipeline.set_state(Gst.State.NULL)
            self.notify("done", {
                "ok": finalized and not error_text,
                "path": str(self.path),
                "mic_path": str(self.mic_path) if self.mic_path else None,
                "error": error_text,
            })


class RecorderApp:
    def __init__(self, root):
        self.root = root
        self.root.title(APP_TITLE)
        self.root.geometry("660x665")
        self.root.minsize(610, 600)
        self.config = load_settings()
        self.updates = queue.Queue()
        self.worker = None
        self.started_at = None
        self.closing = False
        self.refreshing = False
        self.reset_device_selection_pending = False
        self.device_maps = {"system": {DEFAULT_DEVICE: ""},
                            "mic": {DEFAULT_DEVICE: ""}}

        c = self.config
        self.folder = tk.StringVar(value=c.folder)
        self.api = tk.StringVar(value=c.api)
        self.monitor = tk.StringVar(value=str(c.monitor))
        self.fps = tk.StringVar(value=str(c.fps))
        self.video_bitrate = tk.StringVar(value=str(c.video_bitrate))
        self.preset = tk.StringVar(value=c.preset)
        self.threads = tk.StringVar(value=str(c.encoder_threads))
        self.resolution = tk.StringVar(value=c.resolution)
        self.custom_resolution = tk.StringVar(value=c.custom_resolution)
        self.cursor = tk.BooleanVar(value=c.cursor)
        self.container = tk.StringVar(value=c.container)
        self.audio_mode = tk.StringVar(value=c.audio_mode)
        self.separate_mic = tk.BooleanVar(value=c.separate_mic)
        self.audio_bitrate = tk.StringVar(value=str(c.audio_bitrate))
        self.system_device = tk.StringVar(value=DEFAULT_DEVICE)
        self.mic_device = tk.StringVar(value=DEFAULT_DEVICE)
        self.system_volume = tk.DoubleVar(value=c.system_volume * 100)
        self.mic_volume = tk.DoubleVar(value=c.mic_volume * 100)
        self.status = tk.StringVar(value="Ready")
        self.elapsed = tk.StringVar(value="00:00:00")
        self.output_name = tk.StringVar(value="Auto-generated when recording starts")

        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.after(100, self._poll)
        self.refresh_devices(saved_system=c.system_device, saved_mic=c.mic_device)

    def _build_ui(self):
        outer = ttk.Frame(self.root, padding=10)
        outer.pack(fill="both", expand=True)
        heading = ttk.Frame(outer)
        heading.pack(anchor="w", fill="x")
        ttk.Label(heading, text=APP_NAME, font=("Segoe UI", 17, "bold")).pack(side="left")
        ttk.Label(heading, text=f"v{APP_VERSION}").pack(side="left", padx=(9, 0))
        ttk.Label(outer, text="Windows • DXGI / WGC • H.264 + AAC • MKV / MP4").pack(anchor="w", pady=(0, 5))

        tabs = ttk.Notebook(outer)
        tabs.pack(fill="both", expand=True)
        tab_video = ttk.Frame(tabs, padding=8)
        tab_audio = ttk.Frame(tabs, padding=8)
        tabs.add(tab_video, text="Recording")
        tabs.add(tab_audio, text="Audio")

        video = ttk.LabelFrame(tab_video, text="Video settings", padding=8)
        video.pack(fill="x")
        self._choice(video, 0, "Capture API", self.api, ("dxgi", "wgc"))
        self._choice(video, 1, "Frame rate", self.fps, ("20", "24", "30", "60"))
        ttk.Label(video, text="Resolution").grid(row=2, column=0, sticky="w", pady=2, padx=(0, 12))
        resolution_row = ttk.Frame(video)
        resolution_row.grid(row=2, column=1, columnspan=2, sticky="w", pady=2)
        self.resolution_combo = ttk.Combobox(
            resolution_row, textvariable=self.resolution,
            state="readonly", width=13, values=RESOLUTION_CHOICES
        )
        self.resolution_combo.pack(side="left")
        self.custom_entry = ttk.Entry(
            resolution_row, textvariable=self.custom_resolution, width=17
        )
        self.custom_entry.bind("<Return>", lambda _event: self.save_custom_resolution())
        self.custom_save_button = ttk.Button(
            resolution_row, text="Save custom", command=self.save_custom_resolution
        )
        self._choice(video, 3, "x264 preset", self.preset,
                     ("ultrafast", "superfast", "veryfast"))
        self._choice(video, 4, "Encoder threads", self.threads, ("1", "2", "3", "4"))
        self._choice(video, 5, "Monitor index", self.monitor,
                     ("-1", "0", "1", "2", "3"))
        ttk.Label(video, text="Video bitrate (kbps)").grid(row=6, column=0, sticky="w", pady=2)
        ttk.Entry(video, textvariable=self.video_bitrate, width=16).grid(
            row=6, column=1, sticky="w", pady=2)
        ttk.Checkbutton(video, text="Record mouse cursor", variable=self.cursor).grid(
            row=7, column=0, columnspan=2, sticky="w", pady=3)
        self._choice(video, 8, "Container", self.container, ("mkv", "mp4"))
        ttk.Label(tab_video, text="Native / 30 FPS / ultrafast / 2 threads is the tested baseline.",
                  wraplength=545).pack(anchor="w", pady=4)
        self.resolution_hint = ttk.Label(tab_video, text="", wraplength=545)
        self.resolution_hint.pack(anchor="w")
        self.resolution.trace_add("write", lambda *_: self._update_resolution_controls())
        self._update_resolution_controls()

        audio = ttk.LabelFrame(tab_audio, text="Audio recording", padding=8)
        audio.pack(fill="x")
        self._choice(audio, 0, "Record audio", self.audio_mode,
                     ("none", "system", "mic", "both"))
        self._choice(audio, 1, "AAC bitrate (kbps)", self.audio_bitrate,
                     ("96", "128", "160", "192"))
        ttk.Label(audio, text="Desktop playback device").grid(row=2, column=0, sticky="w", pady=5)
        self.system_combo = ttk.Combobox(audio, textvariable=self.system_device,
                                         state="readonly", width=37, values=(DEFAULT_DEVICE,))
        self.system_combo.grid(row=2, column=1, sticky="ew", pady=5)
        ttk.Label(audio, text="Microphone device").grid(row=3, column=0, sticky="w", pady=5)
        self.mic_combo = ttk.Combobox(audio, textvariable=self.mic_device,
                                      state="readonly", width=37, values=(DEFAULT_DEVICE,))
        self.mic_combo.grid(row=3, column=1, sticky="ew", pady=5)
        self.refresh_btn = ttk.Button(audio, text="Refresh audio devices", command=self.refresh_devices)
        self.refresh_btn.grid(row=4, column=0, columnspan=2, sticky="w", pady=8)
        audio.columnconfigure(1, weight=1)
        ttk.Label(audio, text="System audio volume (live)").grid(row=5, column=0, sticky="w", pady=5)
        self.system_slider = ttk.Scale(audio, from_=0, to=200, variable=self.system_volume,
                                       command=lambda _value: self.set_volume("system_gain", self.system_volume.get()))
        self.system_slider.grid(row=5, column=1, sticky="ew", pady=5)
        ttk.Label(audio, text="Microphone volume (live)").grid(row=6, column=0, sticky="w", pady=5)
        self.mic_slider = ttk.Scale(audio, from_=0, to=200, variable=self.mic_volume,
                                    command=lambda _value: self.set_volume("mic_gain", self.mic_volume.get()))
        self.mic_slider.grid(row=6, column=1, sticky="ew", pady=5)
        self.audio_note = ttk.Label(audio, text="100% = unchanged level; above 100% may clip. Device changes apply to the next recording.",
                                    wraplength=420)
        self.audio_note.grid(row=7, column=0, columnspan=2, sticky="w", pady=(5, 3))
        self.separate_mic_checkbox = ttk.Checkbutton(
            audio, text="Record microphone separately (.wav)", variable=self.separate_mic
        )
        self.separate_mic_checkbox.grid(row=8, column=0, columnspan=2, sticky="w", pady=(8, 2))
        self.separate_mic_hint = ttk.Label(
            audio, text="System audio stays in the video; mic is saved to a matching WAV file. "
                        "Disabled: both sources are mixed into one audio track.",
            wraplength=425
        )
        self.separate_mic_hint.grid(row=9, column=0, columnspan=2, sticky="w", pady=(0, 3))
        self.audio_mode.trace_add("write", lambda *_: self._update_audio_controls())
        self._update_audio_controls()

        destination = ttk.LabelFrame(outer, text="Save recordings to", padding=6)
        destination.pack(fill="x", pady=(6, 4))
        ttk.Entry(destination, textvariable=self.folder).pack(side="left", fill="x", expand=True)
        ttk.Button(destination, text="Browse...", command=self.browse).pack(side="left", padx=(7, 0))
        ttk.Label(outer, textvariable=self.output_name, wraplength=620).pack(anchor="w")

        control = ttk.Frame(outer)
        control.pack(fill="x", pady=(7, 4))
        self.start_button = ttk.Button(control, text="●  Start recording", command=self.start)
        self.start_button.pack(side="left", padx=(0, 8))
        self.stop_button = ttk.Button(control, text="■  Stop / Save", command=self.stop,
                                      state="disabled")
        self.stop_button.pack(side="left")
        self.reset_button = ttk.Button(
            control, text="Reset settings", command=self.reset_settings
        )
        self.reset_button.pack(side="left", padx=(9, 0))
        ttk.Label(control, textvariable=self.elapsed, font=("Consolas", 13, "bold")).pack(side="right")
        ttk.Label(outer, textvariable=self.status, wraplength=620).pack(anchor="w")

    @staticmethod
    def _choice(parent, row, caption, variable, values):
        ttk.Label(parent, text=caption).grid(row=row, column=0, sticky="w", pady=2, padx=(0, 12))
        ttk.Combobox(parent, textvariable=variable, state="readonly", width=18,
                     values=values).grid(row=row, column=1, sticky="w", pady=2)

    def _update_resolution_controls(self):
        resolution = self.resolution.get()
        if resolution == "Custom":
            self.custom_entry.pack(side="left", padx=(8, 0))
            self.custom_save_button.pack(side="left", padx=(5, 0))
            self.resolution_hint.configure(
                text="Custom: type width × height (e.g. 3840 × 2160), then Save custom. "
                     "Even dimensions only."
            )
        else:
            self.custom_entry.pack_forget()
            self.custom_save_button.pack_forget()
            if resolution == "Native":
                hint = "Native resolution (default) avoids scaling and minimizes CPU work."
            elif resolution in RESOLUTION_PRESETS:
                w, h = RESOLUTION_PRESETS[resolution]
                hint = f"{resolution}: {w} × {h}. Scaling may increase CPU usage."
            else:
                hint = "Choose a valid resolution preset."
            self.resolution_hint.configure(text=hint)

    def save_custom_resolution(self):
        try:
            width, height = parse_custom_resolution(self.custom_resolution.get())
            self.custom_resolution.set(f"{width} × {height}")
            save_settings(self.current_settings())
        except (ValueError, OSError) as exc:
            messagebox.showerror(APP_TITLE, str(exc))
            return
        self.status.set(f"Custom resolution saved: {width} × {height}")

    def reset_settings(self):
        if self.worker is not None and self.worker.is_alive():
            messagebox.showinfo(APP_TITLE, "Stop recording before resetting settings.")
            return
        if not messagebox.askyesno(
            APP_TITLE, "Reset ALL settings to their defaults?\nYour recordings will not be deleted."
        ):
            return
        c = Settings()
        self.folder.set(c.folder)
        self.api.set(c.api)
        self.monitor.set(str(c.monitor))
        self.fps.set(str(c.fps))
        self.video_bitrate.set(str(c.video_bitrate))
        self.preset.set(c.preset)
        self.threads.set(str(c.encoder_threads))
        self.resolution.set(c.resolution)
        self.custom_resolution.set(c.custom_resolution)
        self.cursor.set(c.cursor)
        self.container.set(c.container)
        self.audio_mode.set(c.audio_mode)
        self.separate_mic.set(c.separate_mic)
        self.audio_bitrate.set(str(c.audio_bitrate))
        self.system_device.set(DEFAULT_DEVICE)
        self.mic_device.set(DEFAULT_DEVICE)
        self.system_volume.set(c.system_volume * 100)
        self.mic_volume.set(c.mic_volume * 100)
        # A device scan started before reset must not restore old selections.
        self.reset_device_selection_pending = True
        save_settings(c)
        self.status.set("All settings reset to defaults (Native, DXGI, 30 FPS).")

    def _update_audio_controls(self):
        mode = self.audio_mode.get()
        enabled_system = mode in ("system", "both")
        enabled_mic = mode in ("mic", "both")
        # Do not disable sliders during recording; they adjust live volume.
        self.system_combo.configure(state="readonly" if enabled_system else "disabled")
        self.mic_combo.configure(state="readonly" if enabled_mic else "disabled")
        self.system_slider.configure(state="normal" if enabled_system else "disabled")
        self.mic_slider.configure(state="normal" if enabled_mic else "disabled")
        if mode == "both":
            self.separate_mic_checkbox.grid()
            self.separate_mic_hint.grid()
        else:
            self.separate_mic_checkbox.grid_remove()
            self.separate_mic_hint.grid_remove()

    def browse(self):
        path = filedialog.askdirectory(initialdir=self.folder.get() or str(Path.home()))
        if path:
            self.folder.set(path)

    def refresh_devices(self, saved_system=None, saved_mic=None):
        if self.refreshing:
            return
        self.refreshing = True
        self.refresh_btn.configure(state="disabled")
        self.status.set("Scanning WASAPI devices...")
        # Preserve current selections, even when this is manually refreshed.
        sys_id = saved_system if saved_system is not None else self.device_maps["system"].get(self.system_device.get(), "")
        mic_id = saved_mic if saved_mic is not None else self.device_maps["mic"].get(self.mic_device.get(), "")

        def discover():
            try:
                devices = enumerate_wasapi_devices()
                self.updates.put(("devices", (devices, sys_id, mic_id)))
            except Exception as exc:
                self.updates.put(("devices_error", str(exc)))

        threading.Thread(target=discover, daemon=True).start()

    def _populate_devices(self, devices, sys_id, mic_id):
        if self.reset_device_selection_pending:
            sys_id = mic_id = ""
            self.reset_device_selection_pending = False
        for category, combo, variable, selected_id in (
            ("system", self.system_combo, self.system_device, sys_id),
            ("mic", self.mic_combo, self.mic_device, mic_id),
        ):
            entries = {DEFAULT_DEVICE: ""}
            for name, ident in devices[category]:
                label = name
                n = 2
                while label in entries:
                    label = f"{name} ({n})"
                    n += 1
                entries[label] = ident
            self.device_maps[category] = entries
            combo.configure(values=list(entries))
            choice = next((label for label, ident in entries.items()
                           if ident == selected_id), DEFAULT_DEVICE)
            variable.set(choice)
        self.status.set(
            f"Ready • {len(devices['system'])} playback devices, "
            f"{len(devices['mic'])} microphones found"
        )
        self.refreshing = False
        self.refresh_btn.configure(state="normal")
        self._update_audio_controls()

    def set_volume(self, element_name, percent):
        if self.worker is not None and self.worker.is_alive():
            self.worker.commands.put((element_name, max(0.0, min(2.0, float(percent) / 100))))

    def current_settings(self):
        fps = int(self.fps.get())
        video_bitrate = int(self.video_bitrate.get())
        audio_bitrate = int(self.audio_bitrate.get())
        threads = int(self.threads.get())
        monitor = int(self.monitor.get())
        if fps not in (20, 24, 30, 60):
            raise ValueError("Choose 20, 24, 30, or 60 FPS")
        if not 500 <= video_bitrate <= 50000:
            raise ValueError("Video bitrate must be 500–50000 kbps")
        if not 32 <= audio_bitrate <= 320:
            raise ValueError("Audio bitrate must be 32–320 kbps")
        if not 1 <= threads <= 4:
            raise ValueError("Encoder threads must be 1–4")
        if monitor < -1 or monitor > 16:
            raise ValueError("Monitor index must be -1 to 16")
        container = self.container.get()
        if container not in ("mkv", "mp4"):
            raise ValueError("Choose MKV or MP4")
        resolution = self.resolution.get()
        if resolution not in RESOLUTION_CHOICES:
            raise ValueError("Select a valid resolution preset")
        custom_resolution = self.custom_resolution.get().strip()
        if resolution == "Custom":
            width, height = parse_custom_resolution(custom_resolution)
            custom_resolution = f"{width} × {height}"
        folder = self.folder.get().strip()
        if not folder:
            raise ValueError("Select an output folder")
        return Settings(
            folder=folder, api=self.api.get(), monitor=monitor,
            fps=fps, video_bitrate=video_bitrate, preset=self.preset.get(),
            encoder_threads=threads, resolution=resolution,
            custom_resolution=custom_resolution,
            cursor=self.cursor.get(), container=container,
            audio_mode=self.audio_mode.get(), separate_mic=self.separate_mic.get(),
            audio_bitrate=audio_bitrate,
            system_device=self.device_maps["system"].get(self.system_device.get(), ""),
            mic_device=self.device_maps["mic"].get(self.mic_device.get(), ""),
            system_volume=max(0, min(2, self.system_volume.get() / 100)),
            mic_volume=max(0, min(2, self.mic_volume.get() / 100)),
        )

    def start(self):
        if self.worker and self.worker.is_alive():
            return
        try:
            config = self.current_settings()
            output = next_output_filename(
                config.folder, config.container,
                config.audio_mode == "both" and config.separate_mic
            )
        except (ValueError, OSError) as exc:
            messagebox.showerror(APP_TITLE, str(exc))
            return
        save_settings(config)
        self.worker = RecorderWorker(config, output, self.updates)
        self.started_at = None
        self.elapsed.set("00:00:00")
        self.output_name.set(
            output.name + ("  +  " + mic_sidecar_path(output).name
                           if config.audio_mode == "both" and config.separate_mic else "")
        )
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.reset_button.configure(state="disabled")
        self.status.set("Starting...")
        self.worker.start()

    def stop(self):
        if self.worker and self.worker.is_alive():
            self.worker.stop_requested.set()
            self.stop_button.configure(state="disabled")
            self.status.set("Stopping and finalizing recording files...")

    def _poll(self):
        try:
            while True:
                kind, data = self.updates.get_nowait()
                if kind == "started":
                    self.started_at = time.monotonic()
                    self.status.set("Recording • Press Stop / Save to finalize")
                elif kind == "status":
                    self.status.set(data)
                elif kind == "warning":
                    self.status.set("GStreamer warning: " + str(data)[:230])
                elif kind == "done":
                    self.started_at = None
                    self.start_button.configure(state="normal")
                    self.stop_button.configure(state="disabled")
                    self.reset_button.configure(state="normal")
                    if data["ok"]:
                        saved_label = "Saved: " + data["path"]
                        if data.get("mic_path"):
                            saved_label += "  +  " + data["mic_path"]
                        self.status.set(saved_label)
                    else:
                        self.status.set("Recording failed or output files may be incomplete")
                        if not self.closing:
                            messagebox.showerror("Recording error", data["error"] or "Unfinalized recording")
                    if self.closing:
                        self.root.destroy()
                        return
                elif kind == "devices":
                    self._populate_devices(*data)
                elif kind == "devices_error":
                    self.refreshing = False
                    self.refresh_btn.configure(state="normal")
                    self.status.set("Device scan failed: " + str(data)[:220] + ". Default devices remain available.")
        except queue.Empty:
            pass

        if self.started_at is not None:
            seconds = int(time.monotonic() - self.started_at)
            self.elapsed.set(f"{seconds // 3600:02}:{seconds // 60 % 60:02}:{seconds % 60:02}")
        self.root.after(100, self._poll)

    def close(self):
        if self.worker and self.worker.is_alive():
            if not self.closing:
                if not messagebox.askyesno(APP_TITLE, "Stop recording and close?\nAll output files will be finalized first."):
                    return
                self.closing = True
                self.stop()
                self.status.set("Finalizing recording files before exit...")
        else:
            try:
                save_settings(self.current_settings())
            except (ValueError, OSError):
                pass
            self.root.destroy()


def main():
    root = tk.Tk()
    ttk.Style().theme_use("clam")
    RecorderApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
