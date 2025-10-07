# Vercel Portfolio Reporter

这是一个个人资产报告生成器，旨在通过 Serverless Function 部署在 Vercel 上。它会自动抓取最新的股票价格和相关新闻，并以网页形式展示您的资产组合情况。

数据通过 Vercel KV 进行持久化存储，方便您随时编辑和更新您的持仓信息。

## 主要功能

- **自动行情**：自动从新浪财经获取 A 股和港股的实时行情。
- **汇率计算**：自动获取港币到人民币的汇率，统一以人民币计价。
- **新闻聚合**：抓取与您持仓相关的最新公司要闻。
- **在线编辑**：通过网页界面随时更新您的股票持仓和负债信息。
- **数据持久化**：利用 Vercel KV (Redis) 安全地存储您的配置。
- **隐私保护**：敏感的金额和持股数量默认隐藏，可一键切换显示。

## 一键部署

点击下面的按钮，即可将此项目一键部署到您自己的 Vercel 账户：

[![Deploy with Vercel](https://vercel.com/button)](https://vercel.com/new/clone?repository-url=https%3A%2F%2Fgithub.com%2FGaryFu%2Fvercel-portfolio-reporter&integration-ids=oac_V3R1GIpkoJorr6fqyiwdhl17)

部署过程非常简单：

1.  点击上方的 "Deploy" 按钮。
2.  Vercel 会要求您创建一个 Git 仓库，您可以直接使用默认的仓库名。
3.  接下来，系统会提示您创建一个 Vercel KV 数据库。**这是必需的步骤**，请点击创建。
4.  创建完成后，Vercel 会自动链接数据库并将所需的环境变量 (`KV_REDIS_URL` 等) 注入到项目中。
5.  点击 "Deploy"，等待部署完成即可。

## 使用方法

1.  部署成功后，访问您的 Vercel 应用 URL。
2.  初次访问时，页面会显示默认的投资组合。
3.  点击页面右上角的 **编辑按钮** (铅笔图标)。
4.  在弹出的窗口中，修改您的总负债，并更新您的持仓股票列表（代码、名称、数量）。
5.  点击 **“保存更改”**。您的配置将安全地存入 Vercel KV，页面会自动刷新以显示您的个人资产报告。

## 技术栈

- **后端**: Python, Flask
- **前端**: 原生 HTML, CSS, JavaScript
- **数据源**: 新浪财经, 谷歌财经
- **部署**: Vercel Serverless Functions
- **数据库**: Vercel KV (Redis)
