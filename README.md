# **`apod-dl.py`**:  Download pictures from NASA's APOD (Astronomy Picture of the Day) site

## Description

APOD now lives at <https://science.nasa.gov/apod/>; the old `apod.nasa.gov` pages redirect there.  The script walks the calendar from the start page's date back to the first APOD (1995 June 16), downloading several days in parallel (`-j`, default 4), and turns each day's picture into a wallpaper:

- scaled to fit the wallpaper size (default **1920x1080**, set it to your screen resolution with `-s WxH`) on a black background (aspect ratio is kept, nothing is cropped)
- captioned in the lower-right corner with the APOD **title** and **date**, white text on a black box
- saved as JPEG (`<original name>.jpg`)

The size, fonts and caption layout are constants at the top of `apod-dl.py` (`WALLPAPER_SIZE`, `TITLE_FONT`, `DATE_FONT`, ...).  The caption uses DejaVu Sans if it is installed and falls back to Pillow's built-in font otherwise.

## Desktop slideshow (MATE / GNOME)

After every run the script rewrites two files in the image directory:

- **`apod-slideshow.xml`** – a background slideshow that cycles through *all* pictures in the directory in a random order, one every 5 minutes with a 5 second cross-fade.  The desktop's slideshow format cannot say "random picture from this folder", every file has to be listed, so the script lists them for you and shuffles the order on each run.
- **`apod.xml`** – registers the slideshow as a wallpaper named *APOD - Astronomy Picture of the Day* in the Appearance / Background dialog.

To (re)generate them without downloading anything, e.g. after deleting pictures or to get a new random order:
```shell
python3 apod-dl.py -x
```

Use the slideshow as your background:
```shell
# MATE
gsettings set org.mate.background picture-filename "$PWD/apod-images/apod-slideshow.xml"
# GNOME
gsettings set org.gnome.desktop.background picture-uri "file://$PWD/apod-images/apod-slideshow.xml"
```

Render the wallpapers at your screen's resolution (`xrandr | grep primary`) so the caption is never cropped, e.g. `python3 apod-dl.py -s 1920x1200`.  If the wallpaper size does not match the screen, pick the *scaled* picture mode with a black background rather than *zoom*, which crops:
```shell
# MATE
gsettings set org.mate.background picture-options scaled
gsettings set org.mate.background color-shading-type solid
gsettings set org.mate.background primary-color '#000000'
```

Show it in the Background dialog (one-time, needs root):
```shell
sudo cp apod-images/apod.xml /usr/share/mate-background-properties/apod.xml    # MATE
sudo cp apod-images/apod.xml /usr/share/gnome-background-properties/apod.xml   # GNOME
```

Timing is controlled by `SLIDE_SECONDS` and `FADE_SECONDS` at the top of `apod-dl.py`.

By default, all images will be downloaded to a directory named **`apod-images`**.  If directory does not exist, the script will create one.

Additionally, the image filename, image date and image text will be written to file: **`apod-images/album_list.txt`**.  Days listed there whose picture still exists are skipped on later runs without touching the network, so re-running to pick up new days is fast.  Delete a picture (or all of them) to have it downloaded again.

Press `Ctrl + C` while downloading to abort.

## Install Dependencies
```shell
pip install -r requirements.txt
```

## Requirements
- python3
- requests
- beautifulsoup4
- lxml
- tqdm
- Pillow

## Help
```shell
python3 apod-dl.py -h

usage: apod-dl.py [-h] [-u URL] [-d DIR] [-o] [-j JOBS] [-s WxH] [-x]

optional arguments:
  -h, --help         show this help message and exit
  -u URL, --url URL  start url. Start from this page's date and go backwards.  If omitted,
                     will start with today's page.
                     Accepts https://science.nasa.gov/apod/?date=YYMMDD, a per-day
                     https://science.nasa.gov/image-article/apod-... page, or a legacy
                     https://apod.nasa.gov/apod/apYYMMDD.html link
  -d DIR, --dir DIR  name of directory to save files to. If omitted, will create and save files to ./apod-images/
  -o, --oneday       just get the one day's apod and nothing else
  -j JOBS, --jobs JOBS
                     number of days to download in parallel (default: 4)
  -s WxH, --size WxH wallpaper size, use your screen resolution so nothing gets
                     cropped (default: 1920x1080)
  -x, --slideshow-only
                     download nothing, just rewrite apod-slideshow.xml (new random
                     order) and apod.xml from the pictures already in the directory
```

## Sample Usage
#### Download everything from today's date back to the first APOD (1995), 4 days at a time:
```shell
python3 apod-dl.py
```

#### Same, 8 days at a time (please stay reasonable, it is NASA's server):
```shell
python3 apod-dl.py -j 8
```

#### Download from date's url backwards:
```shell
python3 apod-dl.py -u "https://science.nasa.gov/apod/?date=201109"
```
Legacy links still work too:
```shell
python3 apod-dl.py -u "https://apod.nasa.gov/apod/ap201109.html"
```

#### Download today's picture only:
```shell
python3 apod-dl.py -o
```

#### Download date's picture only:
```shell
python3 apod-dl.py -o -u "https://science.nasa.gov/apod/?date=201109"
```

