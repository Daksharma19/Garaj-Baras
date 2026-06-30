# Garaj Baras — Flutter App Setup

## What this app does
- **Route tab**: Enter source → destination + speed → scans IMD radar → tells you if/when rain hits your route
- **Nowcast tab**: Pick any location → shows 2-hour rain forecast in 15-min slots
- Calls the live backend at `https://garaj-baras-api.onrender.com`

---

## Steps to get it on your Android phone

### 1. Install Flutter
Download from https://flutter.dev/docs/get-started/install/windows
- Extract to `C:\flutter`
- Add `C:\flutter\bin` to your PATH

Verify: open a new terminal and run `flutter --version`

### 2. Create the base project
Open a terminal, go to your Desktop, and run:

```
cd "C:\Users\user\Desktop\Garaj Baras"
flutter create garaj_baras_flutter --project-name garaj_baras --org com.garajbaras
```

This generates the full Android/iOS boilerplate. Then the files in this folder
(lib/main.dart, lib/route_screen.dart, etc.) will override the generated ones.

> Flutter will ask to overwrite lib/main.dart — say YES (Y)

### 3. Copy the provided files
The files already in this `garaj_baras_flutter/` folder are your app source.
Flutter create will generate them, then you replace with these.

If flutter create overwrote anything, just re-copy:
- `lib/main.dart`
- `lib/api.dart`
- `lib/route_screen.dart`
- `lib/nowcast_screen.dart`
- `android/app/src/main/AndroidManifest.xml`

### 4. Get dependencies
```
cd "C:\Users\user\Desktop\Garaj Baras\garaj_baras_flutter"
flutter pub get
```

### 5. Build the APK
```
flutter build apk --release
```

The APK will be at:
`build\app\outputs\flutter-apk\app-release.apk`

### 6. Install on your phone
Option A — USB cable:
- Enable USB Debugging on your Android phone
- Connect via USB
- Run: `flutter install`

Option B — File transfer:
- Copy `app-release.apk` to your phone
- Open the file on your phone
- Enable "Install from unknown sources" if prompted
- Install!

---

## Want a web link instead?

Run this to build a web version:
```
flutter build web --release
```

Then drag the `build/web/` folder to https://netlify.com/drop
— you'll get a free URL like `https://random-name.netlify.app`
that works on any phone browser.

---

## Troubleshooting

- **Server cold start**: The backend is on Render free tier. First request may take
  30-60 sec. The app retries automatically up to 4 times.
- **Outside radar coverage**: Only Delhi NCR and Uttar Pradesh are covered by IMD radar.
- **Build errors**: Run `flutter doctor` to check your environment.
