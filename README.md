# RC-Car-Arduino-UNO-Q

This project provides a simple web interface for streaming video from a standard USB webcam connected to the UNO Q board, where the board's Linux system handles the camera feed and forwards it to the server.

## Features
- Lightweight Flask web app
- Live MJPEG-style video feed
- Easy deployment on a Debian server

## Requirements
- Python 3.10+
- pip
- A working video source such as a standard USB webcam or another supported camera source

## Setup

1. Create a virtual environment:
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   ```

2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

3. Run the app:
   ```bash
   python app.py
   ```

4. Open your browser at:
   ```text
   http://YOUR_SERVER_IP:8000/
   ```

## Using a different video source
If your camera is not the default device, start the app with:

```bash
VIDEO_SOURCE=/dev/video0 python app.py
```

## Board-side streaming and control
The repository now includes a lightweight board-side helper that can publish the webcam feed to the server and poll for commands:

- `board_stream_sender.py` captures from the webcam and posts JPEG chunks to `/api/stream/chunk`
- `board_commands.py` polls `/api/board/command` for the next control action

Run them on the UNO Q board's Linux side:

```bash
python3 board_stream_sender.py
python3 board_commands.py
```

Set the server URL and board token if needed:

```bash
SERVER_URL=http://YOUR_SERVER_IP:8000 BOARD_TOKEN=your-token python3 board_stream_sender.py
```

## Production deployment on Debian
For a production setup, run the app behind a reverse proxy such as Nginx and use Gunicorn.

Install Gunicorn:
```bash
pip install gunicorn
```

Run:
```bash
gunicorn --bind 0.0.0.0:8000 app:app
```

## Linux installer scripts
Use the dedicated installers for each host role:

### Board-side machine
Run this on the Arduino UNO Q / Linux board host:

```bash
chmod +x install_board_linux.sh
./install_board_linux.sh
```

This installs the Python dependencies, serial support, webcam-related packages, and starts the board-side helper services.

To auto-select the first working camera device, run:

```bash
VIDEO_DEVICE=auto ./install_board_linux.sh
```

`VIDEO_DEVICE` supports either:
- `auto` or `0` to probe and pick the first camera that can actually return frames
- a numeric index such as `2`
- a Linux path such as `/dev/video2` (this is normalized to index `2` by the installer)

### Server-side machine
Run this on the server that will host the web app and Plex prerequisites:

```bash
chmod +x install_server_linux.sh
./install_server_linux.sh
```

This installs the Flask/Gunicorn dependencies, sets up the web app as a systemd service, and prepares the environment for Plex.

## Notes
- This is a starting point for streaming from an Arduino UNO Q setup where the USB webcam feed is handled by the board's Linux system and forwarded to the server, or from another supported camera source.
- For a real-world project, you may want to replace the current direct camera-stream approach on both the server and the Arduino side with a dedicated video pipeline or a more robust streaming protocol.

