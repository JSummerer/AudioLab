# Fonts

The titles use the system's serif where there is one built for screens (New York in the Mac app window and in
Safari, via `ui-serif`). Everywhere else they use **Source Serif 4 SemiBold**, bundled here so every browser and
system shows the same face, offline:

- `source-serif-4-semibold-latin.woff2`, `source-serif-4-semibold-latin-ext.woff2`: Google Fonts' Latin and
  Latin Extended subsets of Source Serif 4 (Adobe), weight 600, with its optical-size axis.
- License: SIL Open Font License 1.1, see `OFL.txt` (Reserved Font Name "Source").

To change the title font: replace these files and the `@font-face` rules at the top of `css/tokens.css`, and
the name in `--font-serif` there. Nothing else refers to a font by name.
