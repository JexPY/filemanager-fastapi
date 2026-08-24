"""Generate test fixtures for EXIF orientation and Display P3 ICC profile.

Run once inside the Docker test environment:
docker compose run --rm -v "$PWD":/src -w /src test \
    python tests/fixtures/generate_colour_fixtures.py
"""

from __future__ import annotations

import ctypes
from ctypes import POINTER, Structure, byref, c_double, c_uint32, c_void_p
from pathlib import Path

import pyvips

FIXTURES_DIR = Path(__file__).parent


def _build_display_p3_profile_bytes() -> bytes:
    """Synthesize a Display P3 ICC profile via liblcms2."""
    lcms = ctypes.CDLL("liblcms2.so.2")

    class cmsCIExyY(Structure):
        _fields_ = [("x", c_double), ("y", c_double), ("Y", c_double)]

    class cmsCIExyYTRIPLE(Structure):
        _fields_ = [("Red", cmsCIExyY), ("Green", cmsCIExyY), ("Blue", cmsCIExyY)]

    lcms.cmsBuildGamma.argtypes = [c_void_p, c_double]
    lcms.cmsBuildGamma.restype = c_void_p

    lcms.cmsCreateRGBProfileTHR.argtypes = [
        c_void_p,
        POINTER(cmsCIExyY),
        POINTER(cmsCIExyYTRIPLE),
        POINTER(c_void_p),
    ]
    lcms.cmsCreateRGBProfileTHR.restype = c_void_p

    lcms.cmsSaveProfileToMem.argtypes = [c_void_p, c_void_p, POINTER(c_uint32)]
    lcms.cmsSaveProfileToMem.restype = c_uint32

    lcms.cmsCloseProfile.argtypes = [c_void_p]

    # D65 white point
    white_point = cmsCIExyY(0.3127, 0.3290, 1.0)
    # Display P3 color primaries
    primaries = cmsCIExyYTRIPLE(
        cmsCIExyY(0.680, 0.320, 1.0),
        cmsCIExyY(0.265, 0.690, 1.0),
        cmsCIExyY(0.150, 0.060, 1.0),
    )
    # 2.2 gamma curve
    gamma_curve = lcms.cmsBuildGamma(None, 2.2)
    curves = (c_void_p * 3)(gamma_curve, gamma_curve, gamma_curve)

    profile = lcms.cmsCreateRGBProfileTHR(None, byref(white_point), byref(primaries), curves)
    if not profile:
        raise RuntimeError("Failed to create Display P3 ICC profile via liblcms2")

    try:
        mem_size = c_uint32(0)
        lcms.cmsSaveProfileToMem(profile, None, byref(mem_size))
        buf = (ctypes.c_char * mem_size.value)()
        lcms.cmsSaveProfileToMem(profile, buf, byref(mem_size))
        return bytes(buf)
    finally:
        lcms.cmsCloseProfile(profile)


def generate_exif_orientation_6() -> None:
    """Generate a 40x20 JPEG image tagged with EXIF orientation 6 (90 deg CW)."""
    # Deliberately non-square (40x20) so rotation swaps width and height to 20x40.
    img = pyvips.Image.black(40, 20) + [200, 50, 50]
    img = img.copy()
    img.set_type(pyvips.GValue.gint_type, "orientation", 6)
    target = FIXTURES_DIR / "exif_orientation_6.jpg"
    img.write_to_file(str(target))
    print(f"Generated {target}")


def generate_display_p3() -> None:
    """Generate a 40x40 JPEG image tagged with Display P3 profile and RGB (200, 100, 50)."""
    p3_profile = _build_display_p3_profile_bytes()
    # Saturated color [200, 100, 50] in P3 transforms to [216, 93, 23] in sRGB.
    img = pyvips.Image.black(40, 40) + [200, 100, 50]
    img = img.copy()
    img.set_type(pyvips.GValue.blob_type, "icc-profile-data", p3_profile)
    target = FIXTURES_DIR / "display_p3.jpg"
    img.write_to_file(str(target))
    print(f"Generated {target}")


def main() -> None:
    generate_exif_orientation_6()
    generate_display_p3()


if __name__ == "__main__":
    main()
