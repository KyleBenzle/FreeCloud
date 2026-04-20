#!/usr/bin/env sh
cd "$(dirname "$0")" || exit 1

find_python() {
    if command -v python3 >/dev/null 2>&1; then
        printf '%s\n' "python3"
        return 0
    fi

    if command -v python >/dev/null 2>&1; then
        printf '%s\n' "python"
        return 0
    fi

    return 1
}

has_tkinter() {
    "$1" -c "import tkinter" >/dev/null 2>&1
}

install_tkinter() {
    python_cmd="$1"

    echo "The simple FreeCloud window needs Python Tkinter."
    echo "Tkinter is not installed on this computer."
    printf "Try to install it now? [y/N]: "
    read answer

    case "$answer" in
        y|Y|yes|YES)
            ;;
        *)
            return 1
            ;;
    esac

    if command -v apt-get >/dev/null 2>&1; then
        sudo apt-get update && sudo apt-get install -y python3-tk
    elif command -v dnf >/dev/null 2>&1; then
        sudo dnf install -y python3-tkinter
    elif command -v yum >/dev/null 2>&1; then
        sudo yum install -y python3-tkinter
    elif command -v pacman >/dev/null 2>&1; then
        sudo pacman -S --needed tk
    elif command -v zypper >/dev/null 2>&1; then
        sudo zypper install -y python3-tk
    elif command -v brew >/dev/null 2>&1; then
        echo "On macOS with Homebrew, Tkinter support depends on how Python was installed."
        echo "Try: brew install python-tk"
        return 1
    else
        echo "I could not find a supported package manager."
        echo "Install Tkinter manually, then run this again."
        return 1
    fi

    has_tkinter "$python_cmd"
}

python_cmd="$(find_python)"
if [ -z "$python_cmd" ]; then
    echo "Python was not found."
    echo "Install Python 3, then run this file again."
    exit 1
fi

if has_tkinter "$python_cmd"; then
    "$python_cmd" freecloud_ui.py
else
    if install_tkinter "$python_cmd"; then
        "$python_cmd" freecloud_ui.py
    else
        echo "Starting command-line sync instead."
        "$python_cmd" freecloud_cli.py
    fi
fi
