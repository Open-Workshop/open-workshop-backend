# Open Workshop - backend

### Цель данного проекта - позволить скачивать моды всем!

### Проект позволяет скачивать моды для игр с серверов **Valve**.

## Backend сервер
Непосредственно общается с Steam, кэширует моды и отсылает пользователям по запросу.
Кэширование нужно для минимизации обращений к серверам Valve.
Так как частые запросы туда могут расцениваться сервером как бот-активность *(и он будет прав)*.
Из про кэшированных модов сервер составляет список который могут использовать сторонние приложения.
Сервер не позволяет скачивать моды со Steam на прямую.
Вместо этого нужно создать запрос на кеширование и после некоторое время опрашивать сервер о состоянии запроса.
После мод будет добавлен в библиотеку сервера от куда его уже можно скачать.


# Документация

## Общая важная информация

### Структура
Загрузка со Steam реализовано как отдельный модуль.
При запросе пользователя загрузить определенный мод со Steam, сервер скачивает его в первую очередь для себя, для пополнения его БД этим модом.
Пользователь может переодически опрашивать сервер о состоянии его запроса, и как только состояние мода станет `1` или `0` он может начинать скачивание.
Сервер так же имеет свою базу данных модов с полнофункциональным поиском не уступающим Steam.

### Использование
Вы можете использовать сервер как для скачивания модов со Steam, 
так и на основе автоматически постоянно пополняющегося каталога модов сделать клиент который позволяет 
удобно скачивать и обновлять моды конкретно с этого сервера.

### Возможные ошибки в базе данных
БД модов генерируется и обновляется автоматически благодаря запросам пользователей.
Из-за этого есть вероятность неполноты информации о каких-то модах *(так как часть информации добывается путем парсинга страницы)*.
А так же есть вероятность неактуальности некоторых записей, так как актуализация информации происходит во время запроса конкретного мода через `/download/steam/{mod_id}`.
Автоматическое обновление невозможно по причине неоправданного расхода трафика *(из-за чего Steam может забанить по IP)*.

### Поведение сервера при скачивании модов
Сервер не отправляет моды напрямую из Steam, вместо этого он сначала скачивает моды себе занося его в БД.

Сначала идет постановка в очередь `3`. В этот момент мод только находится в списке задач на скачивание.

Непосредственно скачивание мода `2`.

Парсинг страницы мода в Steam и регистрация полной информации о моде в базу данных `1`. 
Во время этого процесса пользователям уже можно скачивать моды.

Загрузка завершена `0`.

### Состояния модов
`0` - загружен.

`1` - можно запрашивать, не до конца провалидирован.

`2` - скачивается.

`3` - в очереди на скачивание

## API-функции

### `/download/steam/{mod_id}`
Нужно передать `ID` мода **Steam**. 
Если у сервера уже есть этот мод - он его отправит как `ZIP` архив со сжатием `ZIP_BZIP2`.
Если у сервера нет этого мода он отправит `JSON` с информацией о постановке мода на скачивание.

**JSON ответы:**

1. **Успешная постановка запроса на скачивание:**
```
{"message": "request added to queue", "error_id": 0, "unsuccessful_attempts": true / false, "updating": true / false}
```
HTTP code: `202`
* `unsuccessful_attempts` - это пометка сообщает о том были ли провальные попытки скачать мод.
* `updating` - если true - это значит, что у сервера был загружен мод, но ваш запрос спровоцировал проверку на 
обновление. Сервер удаляет у себя старую версию мода и начинает загрузку новой.

2. **Сервер запускается:**
```
{"message": "the server is not ready to process requests", "error_id": 1}
```
HTTP code: `103`
* Это ошибка возникает в ситуации когда сервер ещё не успел запустить службу по скачиванию модов,
а на самом сервере этого мода нет.

3. **Мод не найден:**
```
{"message": "this mod was not found", "error_id": 2}}
```
HTTP code: `404`

4. **Сервер уже пытается скачать этот мод:**
```
{"message": "your request is already being processed", "error_id": 3}
```
HTTP code: `102`
* Постоянно спрашивать состояние мода через эту функцию не рекомендую, так как она достаточно медленная. 
Лучше использовать `/info/mod/{mod_id}`.


### `/download/{mod_id}`
Нужно передать `ID` мода. 
Если у сервера уже есть этот мод - он его отправит как `ZIP` архив со сжатием `ZIP_BZIP2`.
Эта самая быстрая команда загрузки, но если на сервере не будет запрашиваемого мода никаких действий по его загрузке предпринято не будет.

**JSON ответы:**

1. **Мода нет на сервере:**
```
{"message": "the mod is not on the server", "error_id": 1}
```
HTTP code: `404`
2. **Мод поврежден:**
```
{"message": "the mod is damaged", "error_id": 2}
```
HTTP code: `404`
* Если по какой-то причине мод есть в БД, но его файла нет в системе, запись в БД будет удалена, а вам сообщено что мод был поврежден и отправлен не будет.


### `/list/mods/`
Возвращает список модов к конкретной игре, которые есть на сервере. Имеет необязательные аргументы:

1. `page_size` *(int)* - количество элементов которое будет возвращено. *(диапазон значений `1...50`)*
2. `page` *(int)* - "страница" которая будет возвращена. Рассчитывается как `page_size * page = offeset`.
3. `sort` *(str)* - режим сортировки таблицы перед фильтрацией.
    Префикс `i` указывает что сортировка должна быть инвертированной.
    По умолчанию от меньшего к большему, с `i` от большего к меньшему.
    1. NAME - сортировка по имени.
    2. SIZE - сортировка по размеру.
    3. DATE_CREATION - сортировка по дате создания.
    4. DATE_UPDATE - сортировка по дате обновления.
    5. DATE_REQUEST - сортировка по дате последнего запроса.
    6. SOURCE - сортировка по источнику.
    7. DOWNLOADS *(по умолчанию)* - сортировка по количеству загрузок.
4. `tags` *(list[int])* - список **ID** тегов которые должны иметь отправленные в ответе игры.
5. `games` *(list[int])* - **ID** игр с которыми связан мод. 
По факту 1 мод связан максимум с 1 игрой, но архитектурой сервера это никак не ограничивается. 
6. `allowed_ids` - если передан хотя бы один элемент, идет выдача конкретно этих модов.
7. `dependencies` *(bool)* - отправлять ли моды имеющие зависимость на другие моды. По умолчанию `False`.
8. `primary_sources` *(list[str])* - фильтрация по первоисточникам. Например: `steam`, `local` и т.п..
9. `name` *(str)* - фильтрация по имени. Необязательно писать полное имя.
10. `short_description` *(bool)* - отправлять ли короткое описание мода в ответе. В длину оно максимум 256 символов. По умолчанию `False`.
11. `description` *(bool)* - отправлять ли полное описание мода в ответе. По умолчанию `False`.
12. `dates` *(bool)* - отправлять ли дату последнего обновления и дату создания в ответе. По умолчанию `False`.
13. `general` *(bool)* - отправлять ли базовые поля *(название, размер, источник, количество загрузок)*. По умолчанию `True`.

**JSON ответ:**
1. Некорректный размер страницы:
```
{"message": "incorrect page size", "error_id": 1}
```
HTTP code: `413`
* Означает что размер страницы не входит в диапазон `1...50`.
2. Сложный запрос:
```
{"message": "the maximum complexity of filters is 30 elements in sum", "error_id": 2}
```
HTTP code: `413`
* Максимальное суммарное размер фильтров `tags`, `games`, `primary_sources` не должно превышать 30 элементов.
3. Нормальный ответ:
```
{"database_size": int, "offeset": int, "results": list[dict]}
```
HTTP code: `200`
* `database_size` - общий размер базы данных с текущими фильтрами *(`game_id` и `source`)*.
* `offeset` - итоговый рассчитанный сдвиг с `0` элемента в **БД**.
* `results` - возвращает массив массивов в котором содержатся все элементы соответствующие текущему запросу *(пустой список если ничего не найдено)*.
Содержание под массива:
* * 1. `id` *(int)* - id мода.
* * 2. `size` *(int)* - размер мода в байтах.
* * 3. `source` *(str)* - первоисточник.
* * 4. `name` *(str)* - название мода.
* * 5. `downloads` *(int)* - количество загрузок данного мода.
* * Со 2 по 5 присылаются только если `general=True`*
* * Доступно при `description=True`:
* * 6. `description` *(str)* - описание мода.
* * Доступно при `short_description=True`:
* * 7. `short_description` *(str)* - короткое описание мода.
* * Доступно при `dates=True`:
* * 8. `date_creation` *(`YYYY-MM-DD HH:MI:SS`)* - дата появления в первоисточнике.
* * 9. `date_update` *(`YYYY-MM-DD HH:MI:SS`)* - дата обновления в первоисточнике.


### `/list/games/`
Возвращает список игр, моды к которым есть на сервере. Имеет необязательные аргументы:

1. `page_size` *(int)* - количество элементов которое будет возвращено.
2. `page` *(int)* - "страница" которая будет возвращена. Рассчитывается как `page_size * page = offeset`.
3. `sort` *(str)* - режим сортировки таблицы перед фильтрацией.
    Префикс `i` указывает что сортировка должна быть инвертированной.
    1. `NAME` - сортировка по имени.
    2. `TYPE` - сортировка по типу *(`game` или `app`)*.
    3. `CREATION_DATE` - сортировка по дате регистрации на сервере.
    4. `MODS_DOWNLOADS` - сортировка по суммарному количеству скачанных модов для игры *(по умолчанию)*.
    5. `MODS_COUNT` - сортировка по суммарному количеству модов для игры.
    6. `SOURCE` - сортировка по источнику.
4. `type_app` *(list[str])* - фильтрация по типу *(`game` или `app`)*.
5. `genres` *(list[int])* - фильтрация по жанрам игр.
6. `primary_sources` *(list[str])* - фильтрация по первоисточникам. Например: `steam`, `local` и т.п..
7. `name` *(str)* - фильтрация по имени. Необязательно писать полное имя.
8. `short_description` - отправлять ли короткое описание. По умолчанию `False`.
9. `description` - отправлять ли описание. По умолчанию `False`.
10. `dates` - отправлять ли даты. По умолчанию `False`.
11. `statistics` - отправлять ли статистику. По умолчанию `False`.

**JSON ответ:**
1. Некоректный размер страницы:
```
{"message": "incorrect page size", "error_id": 1}
```
HTTP code: `413`
* Означает что размер страницы не входит в диапазон `1...50`.
2. Слишком сложный запрос:
```
{"message": "the maximum complexity of filters is 30 elements in sum", "error_id": 2}
```
HTTP code: `413`
* Максимальное суммарное размер фильтров `type_app`, `genres`, `primary_sources` не должно превышать 30 элементов.
3. Нормальный ответ:
```
{"database_size": int, "offeset": int, "results": list[list]}
```
HTTP code: `200`
* `database_size` - общий размер базы данных с текущими фильтрами.
* `offeset` - итоговый рассчитанный сдвиг с `0` элемента в **БД**.
* `results` - возвращает массив массивов в котором содержатся все элементы соответствующие текущему запросу *(пустой список если ничего не найдено)*.
Содержание под массива:
* * 1. `id` *(int)* - id игры.
* * 2. `name` *(str)* - название игры.
* * 3. `type` *(str)* - тип приложения *(`game` или `app`)*.
* * 4. `logo` *(str)* - `url` ведущий на лого игры.
* * 5. `source` *(str)* - первоисточник.
* * Доступно при `short_description=True`:
* * 6. `short_description` *(str)* - короткое описание игры.
* * Доступно при `description=True`:
* * 7. `description` *(str)* - описание игры.
* * Доступно при `statistics=True`:
* * 8. `mods_downloads` *(int)* - суммарное количество загрузок у связанных с игрой модов.
* * 9. `mods_count` *(int)* - количество связанных модов.
* * Доступно при `dates=True`:
* * 10. `creation_date` *(`YYYY-MM-DD HH:MI:SS.MS`)* - дата регистрации на сервере


### `/list/tags/{game_id}`
Возвращает список тегов закрепленных за игрой и её модами. Нужно передать ID интересующей игры.
Имеет необязательные аргументы:

1. `page_size` *(int)* - размер 1 страницы. Диапазон - 1...50 элементов.
2. `page` *(int)* - номер странице. Не должна быть отрицательной.

**JSON ответы:**

1. Некорректный размер страницы:
```
{"message": "incorrect page size", "error_id": 1}
```
HTTP code: `413`
2. Нормальный ответ:
```
{"database_size": genres_count, "offset": offset, "results": genres}
```
HTTP code: `200`
* Структура подсловаря в `results`:
* 1. `id` *(`int`)* - ID элемента.
* 2. `name` *(`str`)* - название тега.


### `/list/genres`
Возвращает список жанров для игр.
Имеет необязательные аргументы:

1. `page_size` *(int)* - размер 1 страницы. Диапазон - 1...50 элементов.
2. `page` *(int)* - номер странице. Не должна быть отрицательной.

**JSON ответы:**

1. Некорректный размер страницы:
```
{"message": "incorrect page size", "error_id": 1}
```
HTTP code: `413`
2. Нормальный ответ:
```
{"database_size": genres_count, "offset": offset, "results": genres}
```
HTTP code: `200`
* Структура подсловаря в `results`:
* 1. `id` *(`int`)* - ID элемента.
* 2. `name` *(`str`)* - название жанра.


### `/list/resources_mods/{mod_id}`
Возвращает список ресурсов у конкретного мода. Нужно передать ID интересующего мода.
Имеет необязательные аргументы:

1. `page_size` *(int)* - размер 1 страницы. Диапазон - 1...50 элементов.
2. `page` *(int)* - номер странице. Не должна быть отрицательной.
3. `types_resources` *(list[str])* - фильтрация по типам ресурсов. *(`logo` / `screenshot`)*, ограничение - 20 элементов.

**JSON ответы:**

1. Некорректный размер страницы:
```
{"message": "incorrect page size", "error_id": 1}
```
HTTP code: `413`
* Означает что размер страницы не входит в диапазон `1...50`.
2. Слишком сложный запрос:
```
{"message": "the maximum complexity of filters is 30 elements in sum", "error_id": 2}
```
HTTP code: `413`
* Максимальный размер фильтра `types_resources` не должен превышать 30 элементов.
3. Нормальный ответ:
```
{"database_size": int, "offset": int, "results": list[dict]}
```
HTTP code: `200`
* Структура подсловаря в `results`:
* 1. `date_event` *(`YYYY-MM-DD HH:MI:SS.MS`)* - дата последнего изменения записи на сервере.
* 2. `id` *(`int`)* - ID ресурса.
* 3. `url` *(`str`)* - ссылка на ресурс.
* 4. `type` *(`str`)* - тип ресурса *(`logo` / `screenshot`)*.
* 5. `owner_id` *(`int`)* - ID мода-владельца.


### `/info/mod/{mod_id}`
Возвращает информацию об конкретном моде, а так же его состояние на сервере. 
Нужно передать `ID` мода.

Имеет необязательные аргументы:
1. `dependencies` *(bool)* - передать ли список ID модов от которых зависит этот мод *(ограничено 20 элементами)* *(по умолчанию `false`)*.
2. `short_description` *(bool)* - отправлять ли короткое описание мода в ответе. В длину оно максимум 256 символов. По умолчанию `False`.
3. `description` *(bool)* - отправлять ли полное описание мода в ответе. По умолчанию `False`.
4. `dates` *(bool)* - отправлять ли дату последнего обновления и дату создания в ответе. По умолчанию `False`.
5. `general` *(bool)* - отправлять ли базовые поля *(название, размер, источник, количество загрузок)*. По умолчанию `True`.


**JSON ответ:**
1. Нормальный ответ:
```
{"result": dict / null, ...}
```
HTTP code: `200`
* `results` - возвращает либо `null`, либо словарь с результатом поиска. Структура словаря:
* * 1. `name` *(str)* - название мода.
* * 2. `size` *(int)* - размер мода в байтах.
* * 3. `source` *(str)* - первоисточник.
* * 4. `downloads` *(int)* - количество загрузок данного мода.
* * Первые 4 поля доступны при `general=True`*
* * 5. `condition` *(int)* - состояние мода *(`0` - загружен, `1` - можно запрашивать, не до конца провалидирован, `2` - скачивается, `3` - в очереди на скачивание)*.
* * Доступно при `short_description=True`:
* * 6. `short_description` *(str)* - короткое описание мода *(максимум 256 символов)*.
* * Доступно при `description=True`:
* * 7. `description` *(str)* - описание мода.
* * Доступно при `dates=True`:
* * 8. `date_creation` *(`YYYY-MM-DD HH:MI:SS`)* - дата появления в первоисточнике.
* * 9. `date_update` *(`YYYY-MM-DD HH:MI:SS`)* - дата обновления в первоисточнике.

* При `dependencies=true` возвращает дополнительные пункты: `dependencies` *(list[int])* - ограничено 20 элементами.
`dependencies_count` *(int)* - возвращает общее число элементов.


### `/info/game/{game_id}`
Возвращает информацию о конкретной игре. 
Обязательно нужно передать только `ID` игры.
Имеет необязательные аргументы:

1. `short_description` *(bool)* - отправлять ли короткое описание. По умолчанию `False`.
2. `description` *(bool)* - отправлять ли описание. По умолчанию `False`.
3. `dates` *(bool)* - отправлять ли даты. По умолчанию `False`.
4. `statistics` *(bool)* - отправлять ли статистику. По умолчанию `False`.

**JSON ответ:**
1. Нормальный ответ:
```
{"result": list / null}
```
HTTP code: `200`
* `results` - возвращает словарь в котором содержится информация об игре.
Если игра не найдена, возвращает `null`. Содержание словаря:
* * 1. `name` *(str)* - название игры.
* * 2. `type` *(str)* - тип приложения *(`game` или `app`)*.
* * 3. `logo` *(str)* - `url` ведущий на лого игры.
* * 4. `source` *(str)* - первоисточник.
* * Доступно при `short_description=True`:
* * 5. `short_description` *(str)* - короткое описание игры.
* * Доступно при `description=True`:
* * 6. `description` *(str)* - описание игры.
* * Доступно при `dates=True`:
* * 7. `mods_downloads` *(int)* - суммарное количество загрузок у связанных с игрой модов.
* * 8. `mods_count` *(int)* - количество связанных модов.
* * Доступно при `statistics=True`:
* * 9. `creation_date` *(`YYYY-MM-DD HH:MI:SS.MS`)* - дата регистрации на сервере


### `/info/queue/size`
Возвращает размер очереди *(int)*.

**JSON ответ:**
1. Некорректный ответ:
```
-1
```
HTTP code: `200`
* Означает что при подсчете очереди возникла ошибка.
2. Нормальный ответ:
```
int
```
HTTP code: `200`


### `/condition/mod/{ids_array}`
Возвращает информацию о состоянии мода/модов на сервере.
Нужно передать массив с интересующими ID модов. **Диапазон - 1...50 элементов**

**JSON ответ:**
1. Некорректный запрос:
```
{"message": "the size of the array is not correct", "error_id": 1}
```
HTTP code: `413`
* Означает что массив вышел за диапазон в **1...50** элементов.
2. Нормальный ответ:
```
{id: 0/1/2, ...}
```
HTTP code: `200`
* В ответе возвращает только те элементы которые были переданы в запросе И есть на сервере.
* Об состояниях: *(`0` - загружен, `1` - можно запрашивать, не до конца провалидирован, `2` - скачивается, `3` - в очереди на скачивание)*.
* Если мода нет в ответе - значит его нет на сервере.


### `/statistics/delay`
Возвращает информацию о скорости обработки запросов *(измеряется в миллисекундах)*.
В расчеты попадают 20 последних запросов.

**JSON ответ:**
1. Нормальный ответ:
```
{"fast": int, "full": int}
```
HTTP code: `200`
* `fast` - скорость ответа сервера на запрос о моде который есть на сервере.
* `full` - скорость ответа сервера на запрос о моде которого нет на сервере.
Замер идет от момента получения запроса до момента переключение мода на состояние `1` 
*(можно запрашивать, не до конца провалидирован)*.

* Если сервер прислал *(в одном из двух параметров)* `0` - недостаточно данных для статистики по этому пункту.


### `/statistics/hour`
Возвращает подробную статистику о запросах и работе сервера в конкретный день.
Имеет необязательные аргументы:
1. `select_date` *(`YYYY-MM-DD`; `str`)* - день по которому нужна статистика. По умолчанию - сегодня.
2. `start_hour` *(`int`)* - фильтрация по минимальному значению часа *(диапазон 0...23)*. По умолчанию - `0`.
3. `end_hour` *(`int`)* - фильтрация по максимальному значению часа *(диапазон 0...23)*. По умолчанию - `23`.

При фильтрации по часу отсекаются крайние значения, но не указанное.
Т.е. - если указать в `start_hour` и в `end_hour` одно и тоже значение,
то на выходе получите статистику только по этому часу.

**JSON ответ:**
1. Выход начального времени из 24-часового диапазона:
```
{"message": "start_hour exits 24 hour format", "error_id": 1}
```
HTTP code: `412`
2. Выход конечного времени из 24-часового диапазона:
```
{"message": "end_hour exits 24 hour format", "error_id": 2}
```
HTTP code: `412`
3. Противоречивый запрос:
```
{"message": "conflicting request", "error_id": 3}
```
HTTP code: `409`
* Возникает когда начальное время > конечного времени.
4. Нормальный ответ:
```
list[dict]
```
HTTP code: `200`
* Содержание словаря:
* 1. `count` *(int)* - количество чего-то в этот период времени.
* 3. `type` *(str)* - тип поля.
* 4. `date_time` *(`YYYY-MM-DD HH:MI:SS.MS` - где все что меньше часа всегда равно `0`)* - дата и час.


### `/statistics/day`
Возвращает подробную статистику о запросах и работе сервера в конкретный день.

Принимает необязательные параметры:
1. `start_date` *(`YYYY-MM-DD`; `str`)* - день от начала которого нужна статистика *(включительно)*.
По умолчанию = `end_date`-`7 days`.
2. `end_date` *(`YYYY-MM-DD`; `str`)* - день до которого нужна статистика *(включительно)*.
По умолчанию - текущая дата.

При фильтрации по дня отсекаются крайние значения, но не указанные.
Т.е. - если указать в `start_date` и в `end_date` одно и тоже значение,
то на выходе получите статистику только по этому дню.

**JSON ответ:**
1. Противоречивый запрос:
```
{"message": "conflicting request", "error_id": 3}
```
HTTP code: `409`
* Возникает когда начальная дата > конечной даты.
2. Нормальный ответ:
```
list[dict]
```
HTTP code: `200`
* Содержание словаря:
* 1. `count` *(`int`)* - количество чего-то в этот период времени.
* 3. `type` *(`str`)* - тип поля.
* 4. `date` *(`YYYY-MM-DD`)* - дата.


### `/statistics/info/all`
Возвращает общую информацию о состоянии базы данных. Не принимает аргументов.

**JSON ответ:**
1. Нормальный ответ:
```
{"mods": int, "games": int, "genres": int, "mods_tags": int, "mods_dependencies": int, "statistics_days": int, "mods_sent_count": int}
```
HTTP code: `200`
* `mods` *(`int`)* - суммарное по всем играм количество модов на сервере.
* `games` *(`int`)* - количество проиндексированных.
* `genres` *(`int`)* - количество проиндексированных жанров игр.
* `mods_tags` *(`int`)* - суммарное по всем играм количество тегов для модов.
* `mods_dependencies` *(`int`)* - количество модов у которых есть зависимости на другие моды.
* `statistics_days` *(`int`)* - сколько уже дней ведётся статистика.
* `mods_sent_count` *(`int`)* - сколько раз сервер отправил пользователям файлы с модами.


### `/statistics/info/type_map`
Возвращает карту переводов для типов в статистической ветке. Не принимает аргументов.
Определяет на каком языке отправить ответ через поле `Accept-Language` в `headers` запроса.

**JSON ответ:**
1. Нормальный ответ:
```
{"language": select_language, "result": stc.cache_types_data(select_language)}
```
HTTP code: `200`
* `language` *(str)* - выбранный сервером язык. 
Пытается найти самый перевод для языков *(по пользовательскому приоритету)*.
Если не удалось найти язык по пользовательскому приоритету - устанавливается `ru` как значение по умолчанию.
* `result` *(`dict[str] = str`)* - возвращает словарь с переводами для типов которые получаются 
в `/statistics/day` и `/statistics/hour`.


# Установка на свой сервер

Если вы хотите по какой-то причине поднять сервер у себя, вот небольшая заметки :)

1. Убедитесь что вы установили все зависимости из requirements.txt и у вас не возникли ошибки!
2. Для корректного запуска сервера на Linux вам нужно выполнить [этот пункт](https://github.com/wmellema/Py-SteamCMD-Wrapper#prerequisites)
3. Если у вас возникли какие-то проблемы с установкой - пишите мне!

## Установка на Ubuntu:
### Одной командой:
```bash
sudo adduser steam; sudo apt update; sudo apt upgrade; sudo apt-get install lib32stdc++6 -y; sudo apt install git -y; sudo apt install python3.10 -y; sudo apt -y install python3-pip; sudo apt install htop -y; sudo apt install screen -y; cd /home/steam; git clone https://github.com/Miskler/pytorrent.git; cd pytorrent; chmod -R 777 .; pip3 install -r requirements.txt; sudo snap install --classic certbot; sudo certbot certonly --standalone
```
Это займет некоторое время :)
После перейти к шагу 7!

### Последовательно:
1. Создание пользователя: 
```bash
sudo adduser steam
```
2. Обновление всех пакетов: 
```bash
sudo apt update; sudo apt upgrade
```
3. Установка по: *(после рекомендую перезапустить ОС)* 
```bash
sudo apt-get install lib32stdc++6 -y; sudo apt install git -y; sudo apt install python3.10 -y; sudo apt -y install python3-pip; sudo apt install htop -y; sudo apt install screen -y
``` 
4. Установка репозитория: 
```bash
cd /home/steam; git clone https://github.com/Miskler/pytorrent.git; cd pytorrent; chmod -R 777 .
```
5. Установка зависимостей: 
```bash
pip3 install -r requirements.txt
```
6. Установка CertBot *(SSL сертификат)*: 
```bash
sudo snap install --classic certbot; sudo ln -s /snap/bin/certbot /usr/bin/certbot; sudo certbot certonly --standalone
```
7. Файл настроек Gunicorn:
Переименовываем файл `gunicorn_config_sample.py` в `gunicorn_config.py` и заполняем поля `certfile` и `keyfile`.
8. Запуск *(должны находится в каталоге `backend`)*:
```bash
screen -S pytorrent_backend ./start.sh
```
Жмем сочитание клавиш `CTRL + A + D`
После проверяем адрес - `https://YOU_DOMEN.com:8000/docs`. Если все ок вы увидете документацию.
