# wedding photo pages

Each guest household gets a private page at `madluna.ca/w/<slug>/` with their photos, a "save to Photos" button, and a way to send us their own photos and videos. Each thank-you card gets a QR sticker that links there, with the household's password printed under it.

**Privacy:** the photos, `households.csv` (which holds the passwords), `dist/`, `generated/`, `.secrets.json`, `uploads/` and `qr-stickers.pdf` are gitignored, so they never go to GitHub (that repo is public). Only these scripts are committed. Nobody can open a page or any photo on it without that household's password. The pages are uploaded straight to Cloudflare from this folder as their own small Worker (`madluna-wedding`). It only handles `madluna.ca/w/*` and leaves the rest of the site alone.

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
   - `folder`: optional. If a household's photos are in a folder with its own name (like `photos/bella&chloe`), put that name here. Leave it empty to use `photos/<slug>/`.
   - `password`: leave empty, step 2 fills it in (like `maple 482`). Capitals, spaces and dashes don't matter when guests type it.

2. **Make slugs, passwords and folders.**
   ```
   python build.py slugs
   ```
   This fills in a hard-to-guess slug (like `the-smiths-k7f2`) and a password for each household, and makes an empty `photos/<slug>/` folder for each one. Don't change a slug or password after its sticker is printed.

3. **Add the photos.** Use Google Photos face grouping to find each household's photos, download them, and drop the originals into their `photos/<slug>/` folder. JPG, PNG, HEIC, WebP and GIF all work, and GIFs (like the photobooth ones) stay animated. Pages show photos in the order they were taken. Photos over 24 MB are re-saved for the page at the same size in pixels so Cloudflare will take them; your originals aren't changed.

4. **Build the pages.**
   ```
   python build.py build
   ```
   This makes `dist/`, with thumbnails and the full-size originals for every household that has photos. Run it again whenever photos change. Households without photos are listed so nobody gets missed.

5. **Make the sticker sheet.**
   ```
   python build.py labels --outline
   ```
   This makes `qr-stickers.pdf`: each sticker has the QR code, "Scan for your photos from our day", the password, and the household's name in small print. Print one page on plain paper at **100% / actual size**, hold it against a label sheet up to a window to check it lines up, and test-scan a few codes. When it looks right, run it again without `--outline` and print on the label sheets.
   - `--sheet 22806` (default) is 2" square labels. `--sheet 22807` is 2" round and `--sheet 5160` is regular address labels. The margins are best guesses, so always check with `--outline` first.
   - `--only the-smiths-k7f2` reprints one sticker, `--skip 5` starts on a part-used sheet, `--no-names` leaves names off the stickers, and `--no-passwords` leaves passwords off (if you'd rather write them on the cards by hand).

6. **Put it live** (only once everything is checked):
   ```
   python build.py deploy
   ```
   Run this again after every rebuild. It also sets up the private storage for guest uploads (Cloudflare R2). If it says R2 needs turning on, open the Cloudflare dashboard, go to **R2 Object Storage**, and turn it on (Cloudflare may ask for a payment method; the first 10 GB are free). The first time, or if it says "Not logged in", run `npx wrangler login` from a normal (non-network) folder such as your home folder. It opens a browser window to log in to Cloudflare. To take the pages down later, run `npx wrangler delete madluna-wedding` from that same kind of folder.

7. **Collect what guests send.**
   ```
   python build.py uploads --clear
   ```
   This downloads every photo and video guests sent into `uploads/<slug>/`, then removes each one from Cloudflare once it's safely saved here (leave off `--clear` to keep them there too). Run it whenever you like; files already downloaded are skipped. Move any you want on a page into that household's `photos/` folder and rebuild.

Keep `.secrets.json` safe. It holds the keys the site uses. If it's lost, a fresh one is made on the next build and everyone just has to type their password again.

## settings

The top of `build.py` has the wording (headline, default message, sign-off, sticker line), `BATCH_SIZE` (photos per save button, 20 to start), `MAX_UPLOAD_MB` (biggest file a guest can send) and the thumbnail size. The password page is `login.html`. The page design is in `template.html`.

## before printing everything

- Test the save button on an iPhone and an Android phone. Try a household with more than 20 photos. If saving 20 at once struggles, lower `BATCH_SIZE` and rebuild.
- Scan stickers from a real printed sheet.
