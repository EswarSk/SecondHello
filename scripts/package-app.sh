#!/bin/zsh
set -euo pipefail
cd "$(dirname "$0")/.."
swift build -c release
APP=".build/SecondHello.app"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp .build/release/SecondHello "$APP/Contents/MacOS/SecondHello"
cp -R .build/release/SecondHello_SecondHello.bundle "$APP/SecondHello_SecondHello.bundle"
cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict><key>CFBundleExecutable</key><string>SecondHello</string><key>CFBundleIdentifier</key><string>com.secondhello.demo</string><key>CFBundleName</key><string>Second Hello</string><key>CFBundlePackageType</key><string>APPL</string><key>LSMinimumSystemVersion</key><string>14.0</string><key>NSMicrophoneUsageDescription</key><string>Second Hello listens only after explicit consent to create a reviewable conversation transcript.</string><key>NSSpeechRecognitionUsageDescription</key><string>Second Hello turns a consented live conversation into an editable transcript before anything is saved.</string></dict></plist>
PLIST
echo "Created $(pwd)/$APP"
