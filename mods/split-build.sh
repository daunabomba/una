#!/bin/bash
# Helper script to run una with split terminal windows using tmux
# Usage: ./split-build.sh [una arguments...]
#
# This script splits the terminal into two panes:
# - Top pane: una output (una messages)
# - Bottom pane: build log output (tail -f of build logs)

set -e

# Check if tmux is available
if ! command -v tmux &> /dev/null; then
    echo "Error: tmux is not installed. Please install tmux first."
    echo "  sudo apt install tmux  # Debian/Ubuntu"
    echo "  sudo dnf install tmux  # Fedora"
    echo "  sudo pacman -S tmux    # Arch"
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Create a unique session name
SESSION_NAME="una-build-$$"

# Get the build directory (defaults to bld)
BLD_DIR="${UNA_BLD_DIR:-bld}"

# Start a new tmux session in detached mode
tmux new-session -d -s "$SESSION_NAME" -n "una-build"

# Split the window horizontally (top/bottom)
tmux split-window -v -t "$SESSION_NAME"

# Configure the bottom pane to tail build logs
# The bottom pane will watch for new log files and tail them
tmux send-keys -t "$SESSION_NAME:.0" "echo '=== Build Logs (bottom pane will show logs) ==='" Enter

# Run una in the top pane with all arguments
tmux send-keys -t "$SESSION_NAME:.0" "python3 una.py $*" Enter

# Set up the bottom pane to follow build logs
# We'll use a script that watches for log files
WATCH_SCRIPT=$(cat <<'EOF'
#!/bin/bash
# Watch for and tail build log files
BLD_DIR="${UNA_BLD_DIR:-bld}"
LOG_DIR="$BLD_DIR/x32/build_logs"

echo "=== Watching for build logs in $LOG_DIR ==="
echo "Waiting for build to start..."

# Wait for directory to exist
while [ ! -d "$LOG_DIR" ]; do
    sleep 0.5
done

# Use inotifywait or poll for new files
if command -v inotifywait &> /dev/null; then
    # Use inotifywait for efficient watching
    while true; do
        # Get the most recent log file
        LATEST=$(ls -t "$LOG_DIR"/*.txt 2>/dev/null | head -1)
        if [ -n "$LATEST" ]; then
            echo "=== Tailing: $LATEST ==="
            tail -f "$LATEST" 2>/dev/null &
            TAIL_PID=$!
            
            # Wait for new files
            inotifywait -q -e create -e moved_to "$LOG_DIR" 2>/dev/null
            kill $TAIL_PID 2>/dev/null
        else
            sleep 1
        fi
    done
else
    # Fallback: poll for changes
    CURRENT=""
    while true; do
        LATEST=$(ls -t "$LOG_DIR"/*.txt 2>/dev/null | head -1)
        if [ "$LATEST" != "$CURRENT" ] && [ -n "$LATEST" ]; then
            CURRENT="$LATEST"
            clear
            echo "=== Tailing: $CURRENT ==="
            tail -f "$CURRENT" &
            TAIL_PID=$!
            
            # Wait for file to stop being written or new file to appear
            while true; do
                sleep 2
                NEW_LATEST=$(ls -t "$LOG_DIR"/*.txt 2>/dev/null | head -1)
                if [ "$NEW_LATEST" != "$CURRENT" ]; then
                    kill $TAIL_PID 2>/dev/null
                    break
                fi
            done
        fi
        sleep 1
    done
fi
EOF
)

tmux send-keys -t "$SESSION_NAME:.1" "$WATCH_SCRIPT" Enter

# Attach to the session
echo "Starting tmux session: $SESSION_NAME"
echo "  Top pane: una output"
echo "  Bottom pane: build logs"
echo ""
echo "To detach: Ctrl+b, d"
echo "To reattach: tmux attach -t $SESSION_NAME"
echo ""

tmux attach-session -t "$SESSION_NAME"

# Cleanup on exit
trap "tmux kill-session -t $SESSION_NAME 2>/dev/null" EXIT
