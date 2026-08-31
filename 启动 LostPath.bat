@echo off
chcp 65001 >nul
title LostPath
cd /d "%~dp0desktop"
npm start
