# wedding photo pages

Each guest household gets a private page at `madluna.ca/w/<slug>/` with their photos and a "save to Photos" button. Each thank-you card gets a QR sticker that links there.

**Privacy:** the photos, `households.csv`, `dist/` and `qr-stickers.pdf` are gitignored, so they never go to GitHub (that repo is public). Only these scripts are committed. The pages are uploaded straight to Cloudflare from this folder as their own small Worker (`madluna-wedding`). It only handles `madluna.ca/w/*` and leaves the rest of the site alone.

## one-time setup

Run in the **VS Code terminal** (PowerShell), from this `wedding` folder:

```
python -m pip install -r requirements.txt
```

## the steps

All commands run in the **VS Code terminal**, from this `wedding` folder.

1. **List the households.** Fill in `households.csv` (opens in Excel), one row per household. `households.example.csv` shows what a filled-in row looks like.
   - `name`: shown on the page as "Hi, ___!" and in small print on the sticker.
   - `people`: who's in the household, to help you find their faces in Google Photos. Never shown.
   - `message`: optional, replaces the default note on their page.
   - `slug`: leave empty, step 2 fills it in.

2. **Make slugs and folders.**
   ```
   python build.py slugs
   ```
   This fills in a hard-to-guess slug for each household (like `the-smiths-k7f2`) and makes an empty `photos/<slug>/` folder for each one. Don't change a slug after its sticker is printed.

3. **Add the photos.** Use Google Photos face grouping to find each household's photos, download them, and drop the originals into their `photos/<slug>/` folder. JPG, PNG, HEIC, WebP and GIF all work, and GIFs (like the photobooth ones) stay animated. Pages show photos in the order they were taken.

4. **Build the pages.**
   ```
   python build.py build
   ```
   This makes `dist/`, with thumbnails and the full-size originals for every household that has photos. Run it again whenever photos change. Households without photos are listed so nobody gets missed.

5. **Make the sticker sheet.**
   ```
   python build.py labels --outline
   ```
   This makes `qr-stickers.pdf`. Print one page on plain paper at **100% / actual size**, hold it against a label sheet up to a window to check it lines up, and test-scan a few codes. When it looks right, run it again without `--outline` and print on the label sheets.
   - `--sheet 22806` (default) is 2" square labels. `--sheet 22807` is 2" round and `--sheet 5160` is regular address labels. The margins are best guesses, so always check with `--outline` first.
   - `--only the-smiths-k7f2` reprints one sticker, `--skip 5` starts on a part-used sheet, and `--no-names` leaves names off the stickers.

6. **Put it live** (only once everything is checked):
   ```
   python build.py deploy
   ```
   Run this again after every rebuild. The first time, or if it says "Not logged in", run `npx wrangler login` from a normal (non-network) folder such as your home folder. It opens a browser window to log in to Cloudflare. To take the pages down later, run `npx wrangler delete madluna-wedding` from that same kind of folder.

## settings

The top of `build.py` has the wording (headline, default message, sign-off, sticker line), `BATCH_SIZE` (photos per save button, 20 to start) and the thumbnail size. The page design is in `template.html`.

## before printing everything

- Test the save button on an iPhone and an Android phone. Try a household with more than 20 photos. If saving 20 at once struggles, lower `BATCH_SIZE` and rebuild.
- Scan stickers from a real printed sheet.
