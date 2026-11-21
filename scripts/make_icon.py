"""生成应用图标 app.ico（深色圆角 + 青色闪电）。"""

import math
import os

from PIL import Image, ImageDraw, ImageFilter

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", "icon")

S = 512
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img, "RGBA")

# 背景：深色圆角方块 + 顶部青色辉光
d.rounded_rectangle([16, 16, S - 16, S - 16], radius=112, fill=(12, 14, 19, 255))
glow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
gd = ImageDraw.Draw(glow)
gd.ellipse([-60, -140, S + 60, 220], fill=(94, 234, 212, 46))
glow = glow.filter(ImageFilter.GaussianBlur(70))
img.alpha_composite(glow)

# 细边框
d = ImageDraw.Draw(img, "RGBA")
d.rounded_rectangle([16, 16, S - 16, S - 16], radius=112, outline=(94, 234, 212, 70), width=3)

# 闪电
pts = [(292, 96), (176, 282), (250, 282), (222, 416), (344, 214), (266, 214)]
d.polygon(pts, fill=(94, 234, 212, 255))

# 闪电外发光
bolt = Image.new("RGBA", (S, S), (0, 0, 0, 0))
ImageDraw.Draw(bolt).polygon(pts, fill=(94, 234, 212, 120))
bolt = bolt.filter(ImageFilter.GaussianBlur(24))
img.alpha_composite(bolt)

img.save(os.path.join(OUT, "app.ico"), sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)])
img.resize((256, 256), Image.LANCZOS).save(os.path.join(OUT, "app_preview.png"))
print("icon ok")
