@echo off
rem A股AI主题波段监控 - 每日数据更新并推送GitHub Pages (计划任务调用, 也可手动双击)
cd /d "%~dp0"
python -X utf8 update_data.py > update.log 2>&1
if errorlevel 1 exit /b 1

rem 推送到GitHub, 让 cateatcatx.github.io/ai-theme-monitor 页面同步更新
git add data.js data/ >> update.log 2>&1
git diff --cached --quiet && exit /b 0
git commit -m "daily update" >> update.log 2>&1
git pull --rebase origin main >> update.log 2>&1
git push origin main >> update.log 2>&1
exit /b %errorlevel%
