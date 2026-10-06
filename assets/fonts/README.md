# Source Han Webfont Subsets

These variable WOFF subsets are derived from the locally installed official Adobe
Source Han Sans SC VF 2.004 and Source Han Serif SC VF 2.002 fonts (SC face index 2).
They preserve the original 250-900 weight axis and cover the authored slideshow,
speaker notes, runtime labels, and printable ASCII.

The subset identities are renamed to SekaiSync Sans Web and SekaiSync Serif Web
because the SIL Open Font License reserves the name Source for the original fonts.
Original copyright and license metadata remain in the font files.

- Sans: https://github.com/adobe-fonts/source-han-sans
- Serif: https://github.com/adobe-fonts/source-han-serif
- Licenses: OFL-sans.txt and OFL-serif.txt, copied from the upstream repositories.
- manifest.json records source versions, copyrights, glyph coverage, and file hashes.

Run `python build-fonts.py` from the slideshow directory after changing text.
Source font files are not bundled. Use `--sans` and `--serif` to provide alternate
paths to the official TTC files. Rendering and `node build.mjs` need no Python tools.
