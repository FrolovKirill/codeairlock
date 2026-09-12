"""Keep noVNC controls away from VS Code's activity bar; retain user preferences."""
from pathlib import Path
path = Path('/usr/share/novnc/app/ui.js')
source = path.read_text()
old = "WebUtil.readSetting('controlbar_pos') === 'right'"
new = "WebUtil.readSetting('controlbar_pos', 'right') === 'right'"
if source.count(old) != 1:
    raise SystemExit('Unexpected noVNC control-bar initialization; review before building.')
path.write_text(source.replace(old, new))
