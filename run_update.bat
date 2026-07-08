@echo off
rem A股AI主题中期监控 - 每日数据更新 (由Windows计划任务调用, 也可手动双击)
cd /d "%~dp0"
python -X utf8 update_data.py > update.log 2>&1
exit /b %errorlevel%
