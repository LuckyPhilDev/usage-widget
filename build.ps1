# Builds dist\UsageWidget.exe (single file, no console window).
python -m pip install -r requirements.txt pyinstaller==6.22.3
python -c "from PIL import Image, ImageDraw; im = Image.new('RGBA', (256, 256)); ImageDraw.Draw(im).ellipse((24, 24, 232, 232), fill='#4a9eff'); im.save('icon.ico', sizes=[(16, 16), (32, 32), (48, 48), (256, 256)])"
python -m PyInstaller --onefile --noconsole --name UsageWidget --icon icon.ico --hidden-import pystray._win32 usage_widget.py
