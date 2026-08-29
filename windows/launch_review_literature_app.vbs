Set shell = CreateObject("WScript.Shell")
cmd = "wsl.exe -e bash -lc ""cd '/home/rei/desktop/review' && ./scripts/start_review_app.sh"""
shell.Run cmd, 0, False
