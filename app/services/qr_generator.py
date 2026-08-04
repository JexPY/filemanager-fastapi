import io

import pyvips
import segno


def generate_qr_image(content: str, scale: int = 5) -> bytes:
    qr = segno.make(content)

    # Generate SVG buffer
    out = io.BytesIO()
    qr.save(out, kind="svg", scale=scale)
    svg_data = out.getvalue()

    # Rasterize using pyvips
    image = pyvips.Image.new_from_buffer(svg_data, "")

    # Output as PNG
    png_data = image.write_to_buffer(".png")
    return png_data
