### 以文件夹为主，下载多个博主主页视频，设定一次性下载多少个视频（post_count），设置每个视频时长多少可被下载（video_length）,超过该时长的会被忽略
### 下载过后的视频会记录到数据库，在下次运行的时候不会重复下载，会继续上次下载的视频后新发布的视频
### 需要从头下载，需要清除数据库下载记录
### 视频文件命名有规则 时间_描述_id_[标签1][标签2]，用于小红书上传时解析必要信息
### 单次运行下载的视频放到 video_dir_path，并增量添加到 increased_video_dir_path 

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