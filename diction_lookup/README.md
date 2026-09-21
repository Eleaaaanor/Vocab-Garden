# Open Dictionary Lookup

这是一个面向个人词库整理的最小查询程序。它组合以下公开服务：

- FreeDictionaryAPI：英文释义、词性、中文翻译、近义词和词典例句。
- 中文维基词典：仅在中文翻译缺失时补查中文释义。
- Datamuse：补充严格近义词关系。
- Tatoeba：提供自然英文例句及可用的普通话译句。

## 命令行使用

请使用指定的 Python 环境：

```powershell
& 'D:\programming\PPPPPP\PY\python.exe' .\dictionary_lookup.py apple
```

紧凑 JSON：

```powershell
& 'D:\programming\PPPPPP\PY\python.exe' .\dictionary_lookup.py apple --compact
```

## 作为函数使用

单次调用：

```python
from dictionary_lookup import lookup_word

result = lookup_word("apple")
```

多次调用时复用 HTTP 会话：

```python
from dictionary_lookup import DictionaryClient

with DictionaryClient() as client:
    apple = client.lookup("apple")
    happy = client.lookup("happy")
```

`english` 和 `synonyms` 字段始终存在。若没有英文释义或词性，查询会失败；中文意思、近义词或例句在所有来源都没有结果时为空数组，并在 `warnings` 中说明。

## 测试

不联网的解析与输入校验测试：

```powershell
& 'D:\programming\PPPPPP\PY\python.exe' -m unittest -v
```

## 许可证与调用约束

- FreeDictionaryAPI 和 Wiktionary 数据为 CC BY-SA，输出保留来源与许可证。
- Tatoeba 每条例句携带作者、原始页面和许可证。
- Datamuse 对公开应用建议署名；其调用政策可能发生变化。
- 不要删除 `sources`、例句作者和许可证信息。批量使用时应缓存结果、低并发调用，并遵守服务返回的 429/`Retry-After`。
