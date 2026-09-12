# Setting up Valheim Save-Sync (takes about 5 minutes)

This little tool lets us share one Valheim world without needing a
dedicated server. Whoever opens it first becomes the host — everyone else
just joins them through Steam like normal. When the host closes the game,
it saves the world to the cloud automatically for the next person.

## Step 1: Install Python

1. Go to https://www.python.org/downloads/
2. Click the big yellow "Download Python" button.
3. Run the installer.
   **Important:** on the very first installer screen, check the box that
   says **"Add Python to PATH"** at the bottom before clicking Install.
   (Easy to miss — it's unchecked by default.)

## Step 2: Get the app folder

https://drive.google.com/file/d/1m5x4FaE6YZujg-hDl9PRZ9X50_Av2EsP/view?usp=drive_link

Unzip it somewhere easy to find, like your Desktop.

## Step 3: Install the app's requirements

1. Open the folder you just unzipped.
2. Click in the address bar at the top of the folder window, type `cmd`,
   and press Enter. This opens a command prompt already in that folder.
3. Type this and press Enter:
   ```
   pip install -r requirements.txt
   ```
   Wait for it to finish (a minute or so).

## Step 4: Run it

In that same command prompt, type:
```
python main.py
```

The first time, a small popup will ask for your name — type anything,
that's just what shows up for the group. After that, a little icon
appears in your system tray (bottom-right of your screen, near the clock).
Hover over it to see what's happening.

## Using it day to day

- **If someone's already hosting:** the tray icon tells you who. Just
  open Steam and join them like you normally would.
- **If nobody's hosting:** the app downloads the latest save and launches
  Valheim for you. Just click "Start Game" like normal once it opens.
- **When you're done playing:** just close Valheim normally. The app
  handles saving everything to the cloud in the background — you don't
  need to do anything else.

To run it again later, you can just double-click `main.py` in the folder
(as long as Python is installed, no need to repeat Step 3), or re-open the
command prompt and type `python main.py` again.

## If something goes wrong

Send a screenshot of the command prompt window — there's a
log of everything the app did in a file called `sync.log` in the same
folder, which is the first thing to check.