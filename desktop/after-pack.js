// electron-builder 的 afterPack 钩子：把图标嵌进 LostPath.exe。
//
// 为什么要自己做这件事：package.json 里 signAndEditExecutable=false 是必需的——
// electron-builder 的 winCodeSign 包解压时要为 macOS 的 dylib 建符号链接，而 Windows
// 上建符号链接需要管理员或开发者模式。但那个开关**连带禁用了 rcedit**，而 rcedit 正是
// 负责把图标与版本信息写进 exe 的工具。结果是安装包和引擎 exe 都有图标，偏偏用户在
// 开始菜单和任务栏看到的那个 LostPath.exe 还是 Electron 的默认图标。
//
// 时机选 afterPack 而不是构建结束之后：此刻 exe 已生成、NSIS 还没打包，改完正好被
// 打进安装包。放到最后改就只影响解包目录，装出来的还是默认图标。
const path = require('path');
const fs = require('fs');

exports.default = async function afterPack(context) {
  if (context.electronPlatformName !== 'win32') return;

  const exe = path.join(context.appOutDir, `${context.packager.appInfo.productFilename}.exe`);
  const icon = path.resolve(__dirname, '..', 'ico', 'LostPath.ico');
  if (!fs.existsSync(exe)) throw new Error(`afterPack: 找不到 ${exe}`);
  if (!fs.existsSync(icon)) throw new Error(`afterPack: 找不到图标 ${icon}`);

  const rcedit = require('rcedit');
  await rcedit(exe, {
    icon,
    'version-string': {
      CompanyName: 'LostPath',
      FileDescription: 'LostPath — C 盘占用归因与处理',
      ProductName: 'LostPath',
      LegalCopyright: '',
      OriginalFilename: 'LostPath.exe',
    },
  });
  console.log(`  • afterPack 已把图标嵌进 ${path.basename(exe)}`);
};
