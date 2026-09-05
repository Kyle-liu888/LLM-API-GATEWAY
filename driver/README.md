# SQL Server ODBC 驱动

运行网关需要 Microsoft ODBC Driver 18 或 17 for SQL Server。请通过 Microsoft 官方渠道安装。

如需随本地构建包分发自动安装程序，可在取得相应分发授权后，将 `msodbcsql*.msi` 放入本目录。安装包被 Git 忽略，不随源码仓库上传。未放入安装包时，目标机器必须预先安装驱动。

详细说明见 [运行配置](../docs/runtime.md)。
