import pyvips

def validate_and_strip_image(file_data: bytes) -> tuple[bytes, str, int, int]:
    """
    Load image using pyvips, strip metadata (EXIF), and return the optimized bytes
    along with the detected format and dimensions.
    """
    image = pyvips.Image.new_from_buffer(file_data, "")

    width = image.width
    height = image.height

    # Write to optimized webp format. strip=True removes ALL metadata
    # (EXIF/GPS, ICC profile, XMP) on output in a single call.
    optimized_buffer = image.write_to_buffer(".webp", Q=85, strip=True)
    return optimized_buffer, "image/webp", width, height
