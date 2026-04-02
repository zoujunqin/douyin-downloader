### 登录失效，运行以下脚本
```python
py -m tools.cookie_fetcher --config config.yml
```

### 多账号配置下载，运行以下脚本，自动读取配置 multiple_account_config
```python
py ./run.py
```

### 清除下载缓存
```python
py ./clean_multi_account.py --all # 清理所有
py ./clean_multi_account.py -d C:\Users\11714\Desktop\小红书二创\小猫\单次机器素材 # 清理指定文件夹
```