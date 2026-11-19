# Image decoder notices

Crocodile's image tool uses Pillow 13.0.0, licensed MIT-CMU. The release
ships its unmodified wheel in `wheels/`; installation keeps
`pillow-13.0.0.dist-info/licenses/LICENSE`. That file contains both Pillow's
licence and the bundled libraries' complete notices. Do not replace it with
the shorter source-only Pillow licence.

The manylinux x86_64 wheel's notice inventory includes AOM, Brotli, bzip2,
dav1d, FreeType, HarfBuzz, Little CMS, libavif, libjpeg, liblzma, libpng,
libtiff, libwebp, libyuv, OpenJPEG, Raqm, Tcl/Tk, Xau, XCB, Xdmcp, zlib
and Zstandard. Some provide codecs the tool does not accept. The wheel's
notice file, rather than this inventory, carries their terms.

This software is based in part on the work of the Independent JPEG Group
and the FreeType Team. FreeType's site is https://www.freetype.org/.

Python packages retain their own wheel notices, as described in the
[family inventory](https://github.com/the-boring-crocodile/boring-pond/blob/main/docs/third-party.md).
When changing Pillow, inspect the selected wheels' licence metadata and
bundled notices as well as updating the hash lock.
