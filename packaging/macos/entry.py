"""Entry point for the self-contained application and its private downloader subprocess."""

import sys

from audiolab import runtime

runtime.configure_packaged()

if sys.argv[1:2] == ['--audiolab-downloader']:
    from audiolab.media.ytdlp_update import load_downloader

    load_downloader().main(sys.argv[2:])  # a newer downloaded update when usable, else the built-in copy
elif sys.argv[1:] == ['--audiolab-native-smoke']:
    from native_smoke import main

    raise SystemExit(main())
else:
    from audiolab.cli import main

    raise SystemExit(main())
