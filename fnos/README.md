# Sequoia-X for fnOS

> 本 fnOS 应用由 [HarmonyRun/Sequoia-X-fnOS](https://github.com/HarmonyRun/Sequoia-X-fnOS) 社区分支维护，
> 基于原作者 [sngyai/Sequoia-X](https://github.com/sngyai/Sequoia-X) 二次开发，非原作者官方发行版。

此目录是 Sequoia-X 的飞牛 fnOS 原生应用包装。应用使用飞牛提供的 Python 3.12 运行时，x86 Linux 第三方依赖已内置在 FPK 中，不使用 Docker，也不在安装时访问 PyPI。安装后会运行一个定时选股服务和轻量管理页，行情数据库与日志保存在 fnOS 应用数据目录，升级时不会丢失。

历史行情支持东方财富、腾讯证券、新浪财经和 Baostock。选择“自动切换”时，当前数据源连接失败、超时或被限流后，会从失败的股票开始切换到下一数据源，不会丢失之前已经写入的进度。

## 构建

准备飞牛官方 `fnpack` 1.2.3 或更高版本，然后在项目根目录执行：

```bash
./fnos/build-fpk.sh
```

会生成 `fn-sequoia-x_<版本>_native_x86.fpk`，适用于 Intel/AMD 飞牛设备。首次构建会下载约 200 MB 的 Linux x86 Python 依赖，压缩后的 FPK 会明显小于该体积。

构建脚本会拒绝内容为 `[]` 的向导文件。fnOS 1.2.0800 的应用中心读取这类空向导时会触发错误码 `10111`，界面只显示“应用包不符合系统要求”；不需要某阶段向导时，应直接省略对应文件。

## 安装

1. 打开「应用中心 → 设置 → 手动安装应用」，上传原生 x86 FPK。
2. 应用中心会自动检查或安装 Python 3.12 运行时。
3. 在安装向导中填写飞书机器人 Webhook、运行时间、管理页账号密码。
4. 若开启自动回填，首次启动会下载全市场历史行情。
5. 从桌面或应用中心打开 Sequoia-X，可查看状态、日志、各策略选股结果和 AI 分析，并手动执行当日更新或历史回填。

Web 控制台的“设置”页可随时更换飞书 Webhook，或配置任意 OpenAI 兼容大模型的 Base URL、API Key、模型名和补充提示词。API Key、管理密码和 Webhook 保存在权限为 `0600` 的应用私有配置文件中，管理 API 只返回是否已配置，不回显原文。

## 界面预览

![控制台概览](../docs/images/fnos-dashboard-overview.png)

![控制台设置](../docs/images/fnos-dashboard-settings.png)

默认管理页端口为 `18188`。系统使用交易日历判断是否开市，并在北京时间 `16:00`（收盘后一小时）补齐最新行情；数据源和运行时间均可在安装或应用配置向导中修改。卸载时可选择保留或删除数据库和日志。

版本 2.3.4 适用于 x86_64、fnOS 1.2.0800、Python 3.12.4。桌面入口显式使用 HTTP，避免从启用 HTTPS 的 fnOS 面板打开时错误继承为 HTTPS。

## 实机检查

本地构建成功不等于 fnOS 实机验证完成。发布前至少检查：安装、启动、停止、配置修改、升级，以及卸载时的数据保留/删除。
