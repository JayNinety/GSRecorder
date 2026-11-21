# GSRecorder v1.0

A lightweight screen recorder for Windows, written in Python with **Tkinter** and **GStreamer**.

## Features

- Screen capture with **DXGI** (default) or **WGC**.
- H.264 (x264) recording to **MKV** (default) or **MP4**.
- System audio, microphone, both, or no audio.
- Optional **separate microphone WAV file** when recording both audio sources.
- Resolution presets and custom sizes, FPS/bitrate settings, audio device selection, and volume controls.
- Automatic filenames and saved settings.

## Dependencies

- **Windows 10 or newer**.
- **Python 3** with **Tkinter** (`tkinter`, normally included in the Windows Python installer).
- **PyGObject** (`gi`) with GObject Introspection and **GStreamer 1.x** bindings (`Gst-1.0`).
- **GStreamer 1.x** with these plugin families installed/enabled:
  - **Base:** video/audio conversion, scaling, rate adjustment, mixing, volume.
  - **Good:** MKV/MP4 muxers and WAV writer.
  - **Bad:** D3D11 screen capture, WASAPI2 audio capture, H.264 parser, AAC encoder (`voaacenc`).
  - **Ugly:** x264 encoder (`x264enc`).
  - **Libav (alternative):** `avenc_aac` if `voaacenc` is unavailable.

Required elements include `d3d11screencapturesrc`, `d3d11download`, `wasapi2src`, `x264enc`, `h264parse`, `matroskamux`, `mp4mux`, and **either** `voaacenc` or `avenc_aac`. Separate microphone recording also needs `audiorate` and `wavenc`.

Setup references: [GStreamer for Windows](https://gstreamer.freedesktop.org/download/) · [PyGObject on Windows](https://pygobject.gnome.org/getting_started.html).

## Run

Keep `GSRecorder.py` and `GSRecorder.pyw` in the same folder.

- Double-click **`GSRecorder.pyw`** to open the GUI without a console.
- Or launch from CMD / PowerShell:

  ```powershell
  python GSRecorder.py
  ```

Recordings default to `Videos/Recordings`; settings are saved to `%APPDATA%\GSRecorder\settings.json`.

## License

**GPL-2.0-or-later**. Include a `LICENSE` file containing the full GNU General Public License version 2 text when publishing. Third-party dependencies retain their own licenses.
