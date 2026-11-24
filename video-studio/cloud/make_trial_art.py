"""Runs INSIDE MoneyPrinterTurbo on the CI runner. Original, non-personal test slides."""
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

root = Path('/MoneyPrinterTurbo')
out = root / 'storage/local_videos'
out.mkdir(parents=True, exist_ok=True)
font_path = root / 'resource/fonts/BeVietnamPro-Bold.ttf'
font = ImageFont.truetype(str(font_path), 78)
small = ImageFont.truetype(str(font_path), 32)
slides = [
    ('01', 'TẮT THIẾT BỊ', 'Khi không sử dụng', '#153e47', '#94dbbb'),
    ('02', 'ĐÓN ÁNH SÁNG', 'Mở rèm vào ban ngày', '#224f45', '#f0d59e'),
    ('03', 'DÙNG VỪA ĐỦ', 'Chọn mức phù hợp', '#263b57', '#b7cbea'),
]
for index, (number, title, subtitle, background, accent) in enumerate(slides, 1):
    image = Image.new('RGB', (1080, 1920), background)
    draw = ImageDraw.Draw(image)
    draw.ellipse((650, -160, 1250, 440), fill=accent)
    draw.rounded_rectangle((90, 650, 990, 1270), radius=45, fill='#ffffff')
    draw.text((150, 720), number, font=font, fill=background)
    draw.text((150, 900), title, font=font, fill=background)
    draw.text((150, 1040), subtitle, font=small, fill=background)
    draw.text((90, 140), 'VIDEO STUDIO / KIỂM THỬ TÍCH HỢP', font=small, fill='#ffffff')
    draw.text((90, 1480), 'Một thói quen nhỏ. Mỗi ngày.', font=small, fill='#ffffff')
    image.save(out / f'trial-{index}.png')
print('Created three original integration-test slides.')
