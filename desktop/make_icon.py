"""Build paprika.ico for the desktop app from the cafe logo (../logo.jpg)."""
from pathlib import Path

from PIL import Image, ImageDraw, ImageOps

HERE = Path(__file__).parent
logo = Image.open(HERE.parent / "logo.jpg").convert("RGBA")
logo = ImageOps.fit(logo, (256, 256), method=Image.Resampling.LANCZOS)
mask = Image.new("L", logo.size, 0)
ImageDraw.Draw(mask).ellipse((0, 0, 255, 255), fill=255)
icon = Image.new("RGBA", logo.size, (0, 0, 0, 0))
icon.paste(logo, mask=mask)
icon.save(HERE / "paprika.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
print("paprika.ico created")
