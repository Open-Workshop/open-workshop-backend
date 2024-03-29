import os
import time
import tool
import shutil
import threading
import statistics
import steam_tools as stt
import ow_config as config
import sql_data_client as sdc
import sql_statistics_client as stc
from pathlib import Path
from sql_access_errors import access
from fastapi import FastAPI, Request, UploadFile, File, Form
from sqlalchemy import delete, insert, func, asc
from sqlalchemy.orm import sessionmaker
from sqlalchemy.sql.expression import desc
from datetime import datetime, date, timedelta
from pysteamcmdwrapper import SteamCMD, SteamCMDException
from starlette.responses import JSONResponse, FileResponse, RedirectResponse

WORKSHOP_DIR = os.path.join(os.getcwd())
path = 'steam/steamapps/workshop/content/'

# Создание подключения к базе данных
app = FastAPI(
    title="Open Workshop",
    contact={
        "name": "GitHub",
        "url": "https://github.com/Open-Workshop"
    },
    license_info={
        "name": "MPL-2.0 license",
        "identifier": "MPL-2.0",
    },
)
threads: dict = {}
# Количество системных веток (т.е. не тех кто грузит мод)
sis_threads_count = 2
# Разрешенное количество параллельных загрузок со Steam
parallel = 2

todo_download = {}


def todo_exe():
    global todo_download, threads, sis_threads_count, parallel

    if len(todo_download) > 0:  # and len(threads) <= sis_threads_count+parallel:
        tod = todo_download[list(todo_download.keys())[0]]
        name = f"{tod[0]['consumer_app_id']}/{tod[0]['publishedfileid']}"
        threads[name] = threading.Thread(target=mod_dowload, args=(tod[0], datetime.now(),), name=name)
        threads[name].start()
        del todo_download[list(todo_download.keys())[0]]

    time.sleep(1)

    # Создаем новый туду процесс и даем этому умереть
    # Самовызов это рекурсия который со временем переполнит стек
    threads["todo"] = threading.Thread(target=todo_exe, name="todo")
    threads["todo"].start()


@app.on_event("startup")
async def startup_event():
    app.state.max_concurrency = 50  # GPT сказал что это на что-то влияет :D


@app.middleware("http")
async def modify_header(request: Request, call_next):
    response = await call_next(request)
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Expose-Headers"] = "Content-Type,Content-Disposition"
    return response


@app.get("/")
async def main():
    """
    Переадресация на `/docs`
    """
    stc.update("/")
    return RedirectResponse(url="/docs")


@app.get("/download/steam/{mod_id}")
async def mod_dowloader_request(request: Request, mod_id: int, token: str = None):
    """
    Нужно передать `ID` мода **Steam**.
    Если у сервера уже есть этот мод - он его отправит как `ZIP` архив со сжатием `ZIP_BZIP2`.
    Если у сервера уже есть этот мод, но он отмечен непубличным (`public == 2`), то сервер не предоставит его напрямую - запрашивать через микросервис account's!
    Если у сервера нет этого мода он отправит `JSON` с информацией о постановке мода на скачивание.
    Мод добавляется в TO-DO список задач и будет загруен как только придет его очередь.
    """
    stc.update('/download/steam/')

    wait_time = datetime.now()

    global threads
    global path

    # Создание сессии
    Session = sessionmaker(bind=sdc.engine)

    # Выполнение запроса
    session = Session()
    rows = session.query(sdc.Mod).filter(sdc.Mod.id == mod_id).all()

    mod = stt.get_mod(str(mod_id))

    if mod == None:  # Проверяем, существует ли запрашиваемый мод на серверах Steam
        output = stt.checker(rows=rows, steam_path=path, mod_id=mod_id, session=session)
        if output is not None:
            tool.downloads_count_update(session=session, mod=rows)

            stc.create_processing(type="download_steam_ok", time_start=wait_time)
            stc.update("files_sent")
            return output

        stc.create_processing(type="download_steam_error", time_start=wait_time)
        stc.update("mod_not_found_local")
        return JSONResponse(status_code=404, content={"message": "this mod was not found", "error_id": 2})
    elif threads.get(f"{str(mod['consumer_app_id'])}/{str(mod_id)}",
                     None) == True or mod_id in todo_download.keys():  # Проверяем, загружаем ли этот ресурс прямо сейчас
        stc.create_processing(type="download_steam_error", time_start=wait_time)
        return JSONResponse(status_code=102,
                            content={"message": "your request is already being processed", "error_id": 3})

    print(f"{mod_id in todo_download.keys()}", flush=True)

    real_path = path + f'{str(mod["consumer_app_id"])}/{str(mod_id)}'
    zip_path = f'mods/{str(mod["consumer_app_id"])}/{str(mod_id)}.zip'

    updating = False
    if (rows != None and len(rows) > 0) or os.path.isfile(zip_path) or os.path.isdir(
            real_path):  # Проверяем есть ли запись на сервере в каком-либо виде
        if rows != None and len(rows) > 0 and rows[0].public >= 2:
            if not await access(request=request, user_token=token, real_token=config.token_steam_download_mod, func_name="steam download"):
                return JSONResponse(status_code=403, content="Access denied. This case will be reported.")

        if (rows != None and len(rows) > 0) and os.path.isfile(zip_path):  # Если это ZIP архив - отправляем
            mod_update = datetime.fromtimestamp(mod["time_updated"])
            db_datetime = rows[0].date_update

            # Проверка, нужно ли обновить мод
            print(db_datetime, mod_update)
            if db_datetime >= mod_update:  # дата добавления на сервер позже чем последнее обновление (не надо обновлять)
                tool.downloads_count_update(session=session, mod=rows[0])
                stc.create_processing(type="download_steam_ok", time_start=wait_time)
                stc.update("files_sent")
                return FileResponse(zip_path, filename=f"{rows[0].name}.zip")
            else:
                stc.update("updating_mod")
                updating = True
        elif (rows != None and len(rows) > 0) and os.path.isdir(real_path):  # Если это по какой-то причине - папка
            mod_update = datetime.fromtimestamp(mod["time_updated"])
            db_datetime = rows[0].date_update

            # Проверка, нужно ли обновить мод
            print(db_datetime, mod_update)
            if db_datetime >= mod_update:  # дата добавления на сервер позже чем последнее обновление (не надо обновлять)
                # Пытаемся фиксануть проблему
                tool.zipping(game_id=rows[0].id, mod_id=mod_id, target_size=rows[0].size)
                # Шлем пользователю
                tool.downloads_count_update(session=session, mod=rows[0])
                stc.create_processing(type="download_steam_ok", time_start=wait_time)
                stc.update("files_sent")
                return FileResponse(zip_path, filename=f"{rows[0].name}.zip")
            else:
                stc.update("updating_mod")
                updating = True

        # Чистим сервер
        if os.path.isdir(real_path):
            shutil.rmtree(real_path)
        elif os.path.isfile(real_path + '.zip'):
            os.remove(real_path + '.zip')

        if not updating:
            stc.update("damaged_mod")
            session = Session()
            # Если загрузка окончена ошибкой
            delete_statement = delete(sdc.Mod).where(sdc.Mod.id == int(mod_id))
            delete_tags = sdc.mods_tags.delete().where(sdc.mods_tags.c.mod_id == int(mod_id))
            delete_dep = sdc.mods_dependencies.delete().where(
                sdc.mods_dependencies.c.mod_id == int(mod_id))
            delete_resources = delete(sdc.ResourceMod).where(sdc.ResourceMod.owner_id == int(mod_id))
            # Выполнение операции DELETE
            session.execute(delete_statement)
            session.execute(delete_tags)
            session.execute(delete_dep)
            session.execute(delete_resources)
            session.commit()
            session.close()

    if threads["start"].is_alive():  # Проверяем, готов ли сервер обрабатывать запросы
        stc.create_processing(type="download_steam_error", time_start=wait_time)
        return JSONResponse(status_code=103,
                            content={"message": "the server is not ready to process requests", "error_id": 1})

    if not updating:
        insert_statement = insert(sdc.Mod).values(
            id=mod['publishedfileid'],
            name=mod['title'],
            short_description=tool.truncate_text(text=mod['description']),
            description=mod['description'],
            size=mod['file_size'],
            condition=3,
            public=0,
            date_creation=datetime.fromtimestamp(mod['time_created']),
            date_update=datetime.fromtimestamp(mod['time_updated']),
            date_request=datetime.now(),
            source="steam",
            downloads=0,
            game=mod["consumer_app_id"]
        )
        # Выполнение операции INSERT
        session.execute(insert_statement)
    else:
        session.query(sdc.Mod).filter_by(id=int(mod['publishedfileid'])).update({
            "condition": 3,
            "date_request": datetime.now(),
            "date_update": datetime.fromtimestamp(mod['time_updated']),
            "short_description": tool.truncate_text(text=mod['description']),
            "description": mod['description'],
            "size": mod['file_size'],
            "name": mod['title']
        })
    session.commit()

    todo_download[mod_id] = [mod, wait_time, updating]

    session.close()
    # Оповещаем пользователя, что его запрос принят в обработку
    return JSONResponse(status_code=202,
                        content={"message": "request added to queue", "error_id": 0, "updating": updating})


def mod_dowload(mod_data: dict, wait_time):
    steam = SteamCMD("steam_client")
    # Создание сессии
    Session = sessionmaker(bind=sdc.engine)
    # Выполнение запроса
    session = Session()
    session.query(sdc.Mod).filter_by(id=int(mod_data['publishedfileid'])).update(
        {'condition': 2, "date_update": datetime.fromtimestamp(mod_data['time_updated'])})
    session.commit()

    print(f"Поставлена задача на загрузку: {mod_data['consumer_app_id']}/{mod_data['publishedfileid']}", flush=True)

    wait_steam = threading.Thread(target=steam.workshop_update,
                                  args=(mod_data['consumer_app_id'], mod_data['publishedfileid'], WORKSHOP_DIR,),
                                  name=f"{mod_data['publishedfileid']}/steam_wait")
    wait_steam.start()
    # Ждем максимум 1 минуту
    wait_steam.join(timeout=60)

    ok = tool.zipping(game_id=mod_data['consumer_app_id'], mod_id=mod_data['publishedfileid'],
                      target_size=mod_data['file_size'])

    print(f"Загрузка завершена: {mod_data['consumer_app_id']}/{mod_data['publishedfileid']}", flush=True)

    if ok:  # Если загрузка прошла успешно
        stc.update("download_from_steam_ok")

        session.query(sdc.Mod).filter_by(id=int(mod_data['publishedfileid'])).update({'condition': 1})
        session.commit()

        threading.Thread(target=stt.setters, args=(session, mod_data,),
                         name=f"{mod_data['consumer_app_id']}/{mod_data['publishedfileid']}/get_info").start()

        print(f"Процесс загрузки ({mod_data['consumer_app_id']}/{mod_data['publishedfileid']}) завершен! (успешно)")
        stc.create_processing(type="steam_ok", time_start=wait_time)
    else:
        stc.update("download_from_steam_error")
        # Если загрузка окончена ошибкой
        delete_statement = delete(sdc.Mod).where(sdc.Mod.id == int(mod_data['publishedfileid']))
        delete_tags = sdc.mods_tags.delete().where(sdc.mods_tags.c.mod_id == int(mod_data['publishedfileid']))
        delete_dep = sdc.mods_dependencies.delete().where(
            sdc.mods_dependencies.c.mod_id == int(mod_data['publishedfileid']))
        # Выполнение операции DELETE
        session.execute(delete_statement)
        session.execute(delete_tags)
        session.execute(delete_dep)
        session.commit()
        session.close()
        print(f"Процесс загрузки ({mod_data['consumer_app_id']}/{mod_data['publishedfileid']}) завершен! (неудачно)")
        stc.create_processing(type="steam_error", time_start=wait_time)

    global threads
    del threads[f"{mod_data['consumer_app_id']}/{mod_data['publishedfileid']}"]


@app.get("/download/{mod_id}")
async def download(request: Request, mod_id: int, token: str = None):
    """
    Нужно передать `ID` мода.
    Если у сервера уже есть этот мод - он его отправит как `ZIP` архив со сжатием `ZIP_BZIP2`.
    Если у сервера уже есть этот мод, но он отмечен непубличным (`public == 2`), то сервер не предоставит его напрямую - запрашивать через микросервис account's!
    Эта самая быстрая команда загрузки, но если на сервере не будет запрашиваемого мода никаких действий по его загрузке предпринято не будет.
    """
    stc.update("/download/")

    wait_time = datetime.now()

    global path
    global threads

    # Создание сессии
    Session = sessionmaker(bind=sdc.engine)
    session = Session()
    # Выполнение запроса
    rows = session.query(sdc.Mod).filter(sdc.Mod.id == mod_id).all()

    if rows is not None and len(rows) > 0:
        if rows[0].condition >= 2:
            stc.create_processing(type="download_local_error", time_start=wait_time)
            session.close()
            return JSONResponse(status_code=102, content={"message": "this mod is still loading", "error_id": 3})

        if rows[0].public >= 2:
            if not await access(request=request, user_token=token, real_token=config.token_download_mod, func_name="download"):
                return JSONResponse(status_code=403, content="Access denied. This case will be reported.")

        output = stt.checker(rows=rows, steam_path=path, mod_id=mod_id, session=session)
        if output is not None:
            tool.downloads_count_update(session=session, mod=rows[0])
            stc.create_processing(type="download_local_ok", time_start=wait_time)
            session.close()

            stc.update("files_sent")
            return output
        else:
            stc.create_processing(type="download_local_error", time_start=wait_time)
            session.close()
            stc.update("damaged_mod")
            return JSONResponse(status_code=404, content={"message": "the mod is damaged", "error_id": 2, "test": rows})

    stc.create_processing(type="download_local_error", time_start=wait_time)
    session.close()
    stc.update("mod_not_found_local")
    return JSONResponse(status_code=404, content={"message": "the mod is not on the server", "error_id": 1})


@app.get("/update/steam/{mod_id}")
async def mod_data_update(mod_id: int):
    """
    Нужно передать `ID` мода.

    Делает проверку, есть ли в Steam обновления для этого мода.
    """
    stc.update("/update/steam/")

    global threads
    global path

    wait_time = datetime.now()

    # Создание сессии
    Session = sessionmaker(bind=sdc.engine)

    # Выполнение запроса
    session = Session()
    query = session.query(sdc.Mod.date_update, sdc.Mod.date_request).filter(sdc.Mod.id == mod_id)
    query = query.filter(sdc.Mod.condition == 0).first()

    if query != None:
        db_request = query.date_request
        if wait_time - db_request > timedelta(hours=3):
            session.query(sdc.Mod).filter_by(id=mod_id).update({"date_request": wait_time})
            session.commit()

            mod = stt.get_mod(str(mod_id))

            if mod != None:  # Проверяем, существует ли запрашиваемый мод на серверах Steam
                mod_update = datetime.fromtimestamp(mod["time_updated"])
                db_datetime = query.date_update

                # Проверка, нужно ли обновить мод
                print(db_datetime, mod_update)
                if db_datetime < mod_update:  # Проверка надо ли обновляться

                    todo_download[mod_id] = [mod, wait_time, True]

                    session.query(sdc.Mod).filter_by(id=mod_id).update({
                        "condition": 3,
                        "date_update": datetime.fromtimestamp(mod['time_updated']),
                        "short_description": tool.truncate_text(text=mod['description']),
                        "description": mod['description'],
                        "size": mod['file_size'],
                        "name": mod['title']
                    })
                    session.commit()

                    session.close()
                    return JSONResponse(status_code=202, content={"message": "task of updating is set", "error_id": 0})
                else:
                    session.close()
                    return JSONResponse(status_code=208, content={"message": "mod already update", "error_id": 4})
            else:
                session.close()
                return JSONResponse(status_code=404, content={"message": "mod not found on steam", "error_id": 3})
        else:
            session.close()
            return JSONResponse(status_code=425,
                                content={"message": "check for relevance was made earlier", "error_id": 2})
    else:
        session.close()
        return JSONResponse(status_code=404, content={"message": "mod not found on locale", "error_id": 1})


@app.get("/list/mods/")
async def mod_list(page_size: int = 10, page: int = 0, sort: str = "DOWNLOADS", tags=[],
                   game: int = -1, allowed_ids=[], dependencies: bool = False, primary_sources=[], name: str = "",
                   short_description: bool = False, description: bool = False, dates: bool = False,
                   general: bool = True):
    """
    Возвращает список модов к конкретной игре, которые есть на сервере. Не до конца провалидированные моды и не полностью публичные моды в список не попадают.

    1. `page_size` *(int)* - размер 1 страницы. Диапазон - 1...50 элементов.
    2. `page` *(int)* - номер странице. Не должна быть отрицательной.
    3. `short_description` *(bool)* - отправлять ли короткое описание мода в ответе. В длину оно максимум 256 символов. По умолчанию `False`.
    4. `description` *(bool)* - отправлять ли полное описание мода в ответе. По умолчанию `False`.
    5. `dates` *(bool)* - отправлять ли дату последнего обновления и дату создания в ответе. По умолчанию `False`.
    6. `general` *(bool)* - отправлять ли базовые поля *(название, размер, источник, количество загрузок)*. По умолчанию `True`.

    О сортировке:
    Префикс `i` указывает что сортировка должна быть инвертированной.
    По умолчанию от меньшего к большему, с `i` от большего к меньшему.
    1. NAME - сортировка по имени.
    2. SIZE - сортировка по размеру.
    3. CREATION_DATE - сортировка по дате создания.
    4. UPDATE_DATE - сортировка по дате обновления.
    5. REQUEST_DATE - сортировка по дате последнего запроса.
    6. SOURCE - сортировка по источнику.
    7. MOD_DOWNLOADS *(по умолчанию)* - сортировка по количеству загрузок.

    О фильтрации:
    1. `tags` - передать список тегов которые должен содержать мод *(по умолчанию пуст)* *(нужно передать ID тегов)*.
    2. `game` - ID игры за которой закреплен мод *(фильтр работает если `значение > 0`)*.
    3. `allowed_ids` - если передан хотя бы один элемент, идет выдача конкретно этих модов.
    4. `dependencies` - отфильтровывает моды у которых есть зависимости на другие моды. *(булевка)*
    5. `primary_sources` - список допустимых первоисточников.
    6. `name` - поиск по имени. Например `name=Harmony` *(в отличии от передаваемых списков, тут скобки не нужны)*.
    Работает как проверка есть ли у мода в названии определенная последовательности символов.
    """
    stc.update("/list/mods/")

    tags = tool.str_to_list(tags)
    primary_sources = tool.str_to_list(primary_sources)
    allowed_ids = tool.str_to_list(allowed_ids)

    if page_size > 50 or page_size < 1:
        return JSONResponse(status_code=413, content={"message": "incorrect page size", "error_id": 1})
    elif (len(tags) + len(primary_sources) + len(allowed_ids)) > 30:
        return JSONResponse(status_code=413,
                            content={"message": "the maximum complexity of filters is 30 elements in sum",
                                     "error_id": 2})

    # Создание сессии
    Session = sessionmaker(bind=sdc.engine)
    session = Session()
    # Выполнение запроса
    query = session.query(sdc.Mod.id)
    if description:
        query = query.add_columns(sdc.Mod.description)
    if short_description:
        query = query.add_column(sdc.Mod.short_description)
    if dates:
        query = query.add_columns(sdc.Mod.date_update, sdc.Mod.date_creation)
    if general:
        query = query.add_columns(sdc.Mod.name, sdc.Mod.size, sdc.Mod.source, sdc.Mod.downloads)

    query = query.order_by(tool.sort_mods(sort))
    query = query.filter(sdc.Mod.condition == 0)
    query = query.filter(sdc.Mod.public == 0)

    # Фильтрация по тегам
    if len(tags) > 0:
        for tag_id in tags:
            query = query.filter(sdc.Mod.tags.any(sdc.ModTag.id == tag_id))

    # Фильтрация по конкретным ID
    if len(allowed_ids) > 0:
        query = query.filter(sdc.Mod.id.in_(allowed_ids))

    # Фильтрация по играм
    if game > 0:
        query = query.filter(sdc.Mod.game == game)

    # Фильтрация по первоисточникам
    if len(primary_sources) > 0:
        query = query.filter(sdc.Mod.source.in_(primary_sources))

    if dependencies:
        query = query.outerjoin(sdc.mods_dependencies, sdc.Mod.id == sdc.mods_dependencies.c.mod_id).filter(
            sdc.mods_dependencies.c.mod_id == None)

    # Фильтрация по имени
    if len(name) > 0:
        print(len(name))
        query = query.filter(sdc.Mod.name.ilike(f'%{name}%'))

    mods_count = query.count()

    offset = page_size * page
    mods = query.offset(offset).limit(page_size).all()

    session.close()

    output_mods = []
    for mod in mods:
        out = {"id": mod.id}
        if description:
            out["description"] = mod.description
        if short_description:
            out["short_description"] = mod.short_description
        if dates:
            out["date_update"] = mod.date_update
            out["date_creation"] = mod.date_creation
        if general:
            out["name"] = mod.name
            out["size"] = mod.size
            out["source"] = mod.source
            out["downloads"] = mod.downloads

        output_mods.append(out)

    # Вывод результатов
    return {"database_size": mods_count, "offset": offset, "results": output_mods}


@app.get("/list/games/")
async def games_list(page_size: int = 10, page: int = 0, sort: str = "MODS_DOWNLOADS", name: str = "",
                     type_app=[], genres=[], primary_sources=[],
                     short_description: bool = False, description: bool = False, dates: bool = False,
                     statistics: bool = False):
    """
    Возвращает список игр, моды к которым есть на сервере.

    1. "page_size" - размер 1 страницы. Диапазон - 1...50 элементов.
    2. "page" - номер странице. Не должна быть отрицательной.
    3. "short_description" - отправлять ли короткое описание. По умолчанию `False`.
    4. "description" - отправлять ли описание. По умолчанию `False`.
    5. "dates" - отправлять ли даты. По умолчанию `False`.
    6. "statistics" - отправлять ли статистику. По умолчанию `False`.

    О сортировке:
    Префикс `i` указывает что сортировка должна быть инвертированной.
    1. `NAME` - сортировка по имени.
    2. `TYPE` - сортировка по типу *(`game` или `app`)*.
    3. `CREATION_DATE` - сортировка по дате регистрации на сервере.
    4. `MOD_DOWNLOADS` - сортировка по суммарному количеству скачанных модов для игры *(по умолчанию)*.
    5. `MODS_COUNT` - сортировка по суммарному количеству модов для игры.
    6. `SOURCE` - сортировка по источнику.

    О фильтрации:
    1. `name` - фильтрация по имени.
    2. `type_app` - фильтрация по типу *(массив str)*.
    3. `genres` - фильтрация по жанрам (массив id)*.
    4. `primary_sources` - фильтрация по первоисточнику *(массив str)*.
    """
    stc.update("/list/games/")

    genres = tool.str_to_list(genres)
    type_app = tool.str_to_list(type_app)
    primary_sources = tool.str_to_list(primary_sources)

    if page_size > 50 or page_size < 1:
        return JSONResponse(status_code=413, content={"message": "incorrect page size", "error_id": 1})
    elif (len(type_app) + len(genres) + len(primary_sources)) > 30:
        return JSONResponse(status_code=413,
                            content={"message": "the maximum complexity of filters is 30 elements in sum",
                                     "error_id": 2})

    # Создание сессии
    Session = sessionmaker(bind=sdc.engine)
    session = Session()
    # Выполнение запроса
    query = session.query(sdc.Game.id, sdc.Game.name, sdc.Game.type, sdc.Game.logo, sdc.Game.source)
    if description:
        query = query.add_column(sdc.Game.description)
    if short_description:
        query = query.add_column(sdc.Game.short_description)
    if dates:
        query = query.add_column(sdc.Game.creation_date)
    if statistics:
        query = query.add_columns(sdc.Game.mods_count, sdc.Game.mods_downloads)

    query = query.order_by(tool.sort_games(sort))

    # Фильтрация по жанрам
    if len(genres) > 0:
        for genre in genres:
            print(type(genre))
            query = query.filter(sdc.Game.genres.any(id=genre))

            # filtered_games = session.query(Game).filter(Game.genres.any(id=excluded_genre_id))

    # Фильтрация по первоисточникам
    if len(primary_sources) > 0:
        query = query.filter(sdc.Game.source.in_(primary_sources))

    # Фильтрация по типу
    if len(type_app) > 0:
        query = query.filter(sdc.Game.type.in_(type_app))

    # Фильтрация по имени
    if len(name) > 0:
        query = query.filter(sdc.Game.name.ilike(f'%{name}%'))

    mods_count = query.count()
    offset = page_size * page
    games = query.offset(offset).limit(page_size).all()

    output_games = []
    for game in games:
        out = {"id": game.id, "name": game.name, "type": game.type, "logo": game.logo, "source": game.source}
        if description:
            out["description"] = game.description
        if short_description:
            out["short_description"] = game.short_description
        if dates:
            out["creation_date"] = game.creation_date
        if statistics:
            out["mods_count"] = game.mods_count
            out["mods_downloads"] = game.mods_downloads
        output_games.append(out)

    session.close()
    return {"database_size": mods_count, "offset": offset, "results": output_games}


@app.get("/list/tags/{game_id}")
async def list_tags(game_id: int, page_size: int = 10, page: int = 0, name: str = ''):
    """
    Возвращает список тегов закрепленных за игрой и её модами. Нужно передать ID интересующей игры.

    1. `page_size` - размер 1 страницы. Диапазон - 1...50 элементов.
    2. `page` - номер странице. Не должна быть отрицательной.
    """
    stc.update("/list/tags/")

    if page_size > 50 or page_size < 1:
        return JSONResponse(status_code=413, content={"message": "incorrect page size", "error_id": 1})

    # Создание сессии
    Session = sessionmaker(bind=sdc.engine)
    session = Session()
    # Выполнение запроса
    query = session.query(sdc.ModTag)
    query = query.filter(sdc.ModTag.associated_games.any(sdc.Game.id == game_id))
    if len(name) > 0:
        query = query.filter(sdc.ModTag.name.ilike(f'%{name}%'))

    tags_count = query.count()
    offset = page_size * page
    tags = query.offset(offset).limit(page_size).all()

    session.close()
    return {"database_size": tags_count, "offset": offset, "results": tags}


@app.get("/list/tags/mods/{mods_ids_list}")
async def list_tags_for_mods(request: Request, mods_ids_list, token: str = None, tags=[], only_ids: bool = False):
    """
    Возвращает ассоциации модов с тегами.
    Если в переданном списке модов есть ID непубличного мода, то будет отказано в доступе, делать такие запросы через микросервис account!

    1. `mods_ids_list` - список модов к которым нужно вернуть ассоциации (принимает список ID модов).
    2. `tags` - если не пуст возвращает ассоциации конкретно с этими тегами (принимает список ID тегов).
    3. `only_ids` - если True возвращает только ID ассоцируемых тегов, если False возвращает всю информацию о каждом ассоцируемом теге.
    """
    stc.update("/list/tags/mods/")

    mods_ids_list = tool.str_to_list(mods_ids_list)
    tags = tool.str_to_list(tags)

    if (len(mods_ids_list) + len(tags)) > 80:
        return JSONResponse(status_code=413,
                            content={"message": "the maximum complexity of filters is 80 elements in sum",
                                     "error_id": 1})

    # Создание сессии
    Session = sessionmaker(bind=sdc.engine)
    session = Session()

    query = session.query(sdc.Mod.id)
    query = query.filter(sdc.Mod.id.in_(mods_ids_list))
    query = query.filter(sdc.Mod.public >= 2)

    if len(query.all()) > 0:
        if not await access(request=request, user_token=token, real_token=config.token_info_mod, func_name="tags for mods"):
            session.close()
            return JSONResponse(status_code=403, content="Access denied. This case will be reported.")

    # Выполнение запроса
    result = {}
    query_global = session.query(sdc.ModTag).join(sdc.mods_tags)
    for mod_id in mods_ids_list:
        query = query_global.filter(sdc.mods_tags.c.mod_id == mod_id)
        if len(tags) > 0:
            query = query.filter(sdc.ModTag.id.in_(tags))

        if only_ids:
            if result.get(mod_id, None) == None: result[mod_id] = []
            for id in query.all(): result[mod_id].append(id.id)
        else:
            result[mod_id] = query.all()

    return result


@app.get("/list/genres")
async def list_genres(page_size: int = 10, page: int = 0, name: str = ''):
    """
    Возвращает список жанров для игр.

    1. `page_size` - размер 1 страницы. Диапазон - 1...50 элементов.
    2. `page` - номер странице. Не должна быть отрицательной.
    """
    stc.update("/list/genres/")

    if page_size > 50 or page_size < 1:
        return JSONResponse(status_code=413, content={"message": "incorrect page size", "error_id": 1})

    # Создание сессии
    Session = sessionmaker(bind=sdc.engine)
    session = Session()
    # Выполнение запроса
    query = session.query(sdc.Genres)
    if len(name) > 0:
        query = query.filter(sdc.Genres.name.ilike(f'%{name}%'))

    genres_count = query.count()
    offset = page_size * page
    genres = query.offset(offset).limit(page_size).all()

    session.close()
    return {"database_size": genres_count, "offset": offset, "results": genres}


@app.get("/list/genres/games/{games_ids_list}")
async def list_genres_for_games(games_ids_list, genres=[], only_ids: bool = False):
    """
    Возвращает ассоциации игр с жанрами

    1. `games_ids_list` - список игр к которым нужно вернуть ассоциации (принимает список ID игр).
    2. `genres` - если не пуст возвращает ассоциации конкретно с этими жанрами (принимает список ID жанров).
    3. `only_ids` - если True возвращает только ID ассоцируемых жанров, если False возвращает всю информацию о каждом ассоцируемом жанре.
    """
    stc.update("/list/genres/games/")

    games_ids_list = tool.str_to_list(games_ids_list)
    genres = tool.str_to_list(genres)

    if (len(games_ids_list) + len(genres)) > 80:
        return JSONResponse(status_code=413,
                            content={"message": "the maximum complexity of filters is 80 elements in sum",
                                     "error_id": 2})

    # Создание сессии
    Session = sessionmaker(bind=sdc.engine)
    session = Session()

    # Выполнение запроса
    result = {}
    query_global = session.query(sdc.Genres).join(sdc.game_genres)
    for game_id in games_ids_list:
        query = query_global.filter(sdc.game_genres.c.game_id == game_id)
        if len(genres) > 0:
            query = query.filter(sdc.Genres.id.in_(genres))

        if only_ids:
            if result.get(game_id, None) == None: result[game_id] = []
            for id in query.all(): result[game_id].append(id.id)
        else:
            result[game_id] = query.all()

    return result


@app.get("/list/resources_mods/{mods_list_id}")
async def list_resources_mods(request: Request, mods_list_id, token: str = None, page_size: int = 10, page: int = 0, types_resources=[]):
    """
    Возвращает список ресурсов у конкретного мода/списка модов.
    Если в переданном списке модов есть ID непубличного мода, то будет отказано в доступе, делать такие запросы через микросервис account!

    1. `page_size` *(int)* - размер 1 страницы. Диапазон - 1...70 элементов.
    2. `page` *(int)* - номер страницы. Не должна быть отрицательной.
    3. `types_resources` *(list[str])* - фильтрация по типам ресурсов. *(`logo` / `screenshot`)*, ограничение - 80 элементов.
    """
    stc.update("/list/resources_mods/")

    if page_size > 70 or page_size < 1:
        return JSONResponse(status_code=413, content={"message": "incorrect page size", "error_id": 1})

    types_resources = tool.str_to_list(types_resources)
    mods_list_id = tool.str_to_list(mods_list_id)

    if len(types_resources) + len(mods_list_id) > 80:
        return JSONResponse(status_code=413,
                            content={"message": "the maximum complexity of filters is 80 elements in sum",
                                     "error_id": 2})

    # Создание сессии
    Session = sessionmaker(bind=sdc.engine)
    session = Session()

    query = session.query(sdc.Mod.id)
    query = query.filter(sdc.Mod.id.in_(mods_list_id))
    query = query.filter(sdc.Mod.public >= 2)

    if len(query.all()) > 0:
        if not await access(request=request, user_token=token, real_token=config.token_info_mod, func_name="resources for mods"):
            session.close()
            return JSONResponse(status_code=403, content="Access denied. This case will be reported.")

    # Выполнение запроса
    query = session.query(sdc.ResourceMod)
    query = query.filter(sdc.ResourceMod.owner_id.in_(mods_list_id))

    # Фильтрация по типу
    if len(types_resources) > 0:
        query = query.filter(sdc.ResourceMod.type.in_(types_resources))

    resources_count = query.count()
    offset = page_size * page
    resources = query.offset(offset).limit(page_size).all()

    session.close()
    return {"database_size": resources_count, "offset": offset, "results": resources}


@app.get("/list/resources/{resources_list_id}")
async def list_resources_mods(request: Request, resources_list_id, token: str = None):
    """
    Возвращает список ресурсов по их id. Список в размере не должен быть > 80!
    Если в переданном списке ресурсов есть ID привязанное к непубличному моду, то будет отказано в доступе!
    """
    stc.update("/list/resources/")

    resources_list_id = tool.str_to_list(resources_list_id)

    if len(resources_list_id) > 80:
        return JSONResponse(status_code=413,
                            content={"message": "the maximum complexity of filters is 80 elements in sum",
                                     "error_id": 2})

    # Создание сессии
    Session = sessionmaker(bind=sdc.engine)
    session = Session()

    # Выполнение запроса
    query = session.query(sdc.ResourceMod)
    query = query.filter(sdc.ResourceMod.id.in_(resources_list_id))

    resources_count = query.count()
    resources = query.all()

    # Проверка правомерности
    if resources_count > 0:
        mods_ids_check = []
        for i in resources:
            mods_ids_check.append(i.owner_id)

        query = session.query(sdc.Mod.id)
        query = query.filter(sdc.Mod.id.in_(mods_ids_check))
        query = query.filter(sdc.Mod.public >= 2)

        if len(query.all()) > 0:
            if not await access(request=request, user_token=token, real_token=config.token_info_mod, func_name="resources list"):
                session.close()
                return JSONResponse(status_code=403, content="Access denied. This case will be reported.")

    # Возврат успешного результата
    session.close()
    return {"database_size": resources_count, "results": resources}


@app.get("/info/game/{game_id}")
async def game_info(game_id: int, short_description: bool = False, description: bool = False, dates: bool = False,
                    statistics: bool = False):
    """
    Возвращает информацию об конкретной игре, а так же его состояние на сервере.

    1. `short_description` *(bool)* - отправлять ли короткое описание. По умолчанию `False`.
    2. `description` *(bool)* - отправлять ли описание. По умолчанию `False`.
    3. `dates` *(bool)* - отправлять ли даты. По умолчанию `False`.
    4. `statistics` *(bool)* - отправлять ли статистику. По умолчанию `False`.
    """
    stc.update("/info/game/")

    # Создание сессии
    Session = sessionmaker(bind=sdc.engine)
    session = Session()

    # Выполнение запроса
    query = session.query(sdc.Game.name, sdc.Game.type, sdc.Game.logo, sdc.Game.source)
    if description:
        query = query.add_column(sdc.Game.description)
    if short_description:
        query = query.add_column(sdc.Game.short_description)
    if dates:
        query = query.add_column(sdc.Game.creation_date)
    if statistics:
        query = query.add_columns(sdc.Game.mods_count, sdc.Game.mods_downloads)

    query = query.filter(sdc.Game.id == game_id)
    output = {"pre_result": query.first()}
    session.close()

    if output["pre_result"]:
        output["result"] = {"name": output["pre_result"].name, "type": output["pre_result"].type,
                            "logo": output["pre_result"].logo, "source": output["pre_result"].source}
        if description:
            output["result"]["description"] = output["pre_result"].description
        if short_description:
            output["result"]["short_description"] = output["pre_result"].short_description
        if dates:
            output["result"]["creation_date"] = output["pre_result"].creation_date
        if statistics:
            output["result"]["mods_count"] = output["pre_result"].mods_count
            output["result"]["mods_downloads"] = output["pre_result"].mods_downloads
    else:
        output["result"] = None
    del output["pre_result"]

    return output


@app.get("/info/mod/{mod_id}")
async def mod_info(request: Request, mod_id: int, token: str = None, dependencies: bool = False, short_description: bool = False, description: bool = False,
                   dates: bool = False, general: bool = True, game: bool = False):
    """
    Возвращает информацию о конкретном моде.
    Если у сервера уже есть этот мод, но он отмечен непубличным (`public == 2`), то сервер не предоставит его напрямую - запрашивать через микросервис account's!

    1. `mod_id` *(int)* - id мода.
    2. `dependencies` *(bool)* - передать ли список ID модов от которых зависит этот мод. (ограничено 20 элементами)
    3. `short_description` *(bool)* - отправлять ли короткое описание мода в ответе. В длину оно максимум 256 символов. По умолчанию `False`.
    4. `description` *(bool)* - отправлять ли полное описание мода в ответе. По умолчанию `False`.
    5. `dates` *(bool)* - отправлять ли дату последнего обновления и дату создания в ответе. По умолчанию `False`.
    6. `general` *(bool)* - отправлять ли базовые поля *(название, размер, источник, количество загрузок)*. По умолчанию `True`.
    7. `game` *(bool)* - отправлять ли краткую информацию *(id+название)* об игре-владельце. По умолчанию `False`.


    Я не верю что в зависимостях мода будет более 20 элементов, поэтому такое ограничение.
    Но если все-таки такой мод будет, то без ограничения мой сервер может лечь от нагрузки.
    """
    stc.update("/info/mod/")

    output = {}

    # Создание сессии
    Session = sessionmaker(bind=sdc.engine)
    session = Session()

    # Выполнение запроса
    query = session.query(sdc.Mod.condition)
    if description:
        query = query.add_columns(sdc.Mod.description)
    if short_description:
        query = query.add_column(sdc.Mod.short_description)
    if dates:
        query = query.add_columns(sdc.Mod.date_update, sdc.Mod.date_creation)
    if general:
        query = query.add_columns(sdc.Mod.name, sdc.Mod.size, sdc.Mod.source, sdc.Mod.downloads)
    if game:
        query = query.add_columns(sdc.Mod.game)

    query = query.add_columns(sdc.Mod.public)
    query = query.filter(sdc.Mod.id == mod_id)
    output["pre_result"] = query.first()

    if not output["pre_result"]:
        return JSONResponse(status_code=404, content="Mod not found.")

    if output["pre_result"].public >= 2:
        if not await access(request=request, user_token=token, real_token=config.token_info_mod, func_name="info mod"):
            session.close()
            return JSONResponse(status_code=403, content="Access denied. This case will be reported.")

    if dependencies:
        query = session.query(sdc.mods_dependencies.c.dependence)
        query = query.filter(sdc.mods_dependencies.c.mod_id == mod_id)

        count = query.count()
        result = query.limit(20).all()
        output["dependencies"] = [row[0] for row in result]
        output["dependencies_count"] = count

    if game:
        result = session.query(sdc.Game.name).filter(sdc.Game.id == output["pre_result"].game).first()

        output["game"] = {"id": output["pre_result"].game, "name": result.name}

    # Закрытие сессии
    session.close()

    if output["pre_result"]:
        output["result"] = {"condition": output["pre_result"].condition}
        if description:
            output["result"]["description"] = output["pre_result"].description
        if short_description:
            output["result"]["short_description"] = output["pre_result"].short_description
        if dates:
            output["result"]["date_update"] = output["pre_result"].date_update
            output["result"]["date_creation"] = output["pre_result"].date_creation
        if general:
            output["result"]["name"] = output["pre_result"].name
            output["result"]["size"] = output["pre_result"].size
            output["result"]["source"] = output["pre_result"].source
            output["result"]["downloads"] = output["pre_result"].downloads
            output["result"]["public"] = output["pre_result"].public
        if game:
            output["result"]["game"] = output["game"]
            del output["game"]
    else:
        output["result"] = None
    del output["pre_result"]

    return output


@app.get("/info/queue/size")
async def queue_size():
    """
    Возвращает размер очереди *(int)*.
    """
    stc.update("/info/queue/size/")

    try:
        size = len(todo_download)
        if size != 0:
            size = round(size / parallel)
    except:
        size = -1

    return size


@app.get("/condition/mod/{ids_array}")
async def condition_mods(ids_array):
    """
    Возвращает список с состояниями существующих модов на сервере.
    Принимает массив ID модов. Возвращает словарь с модами которые есть на сервере и их состоянием *(`0`, `1`, `2`, `3`)*.
    Ограничение на разовый запрос - 50 элементов.
    """
    stc.update("/condition/mod/")

    ids_array = tool.str_to_list(ids_array)

    if len(ids_array) < 1 or len(ids_array) > 50:
        return JSONResponse(status_code=413, content={"message": "the size of the array is not correct", "error_id": 1})

    output = {}

    # Создание сессии
    Session = sessionmaker(bind=sdc.engine)
    session = Session()

    # Выполнение запроса
    query = session.query(sdc.Mod)
    query = query.filter(sdc.Mod.id.in_(ids_array))
    for i in query:
        output[i.id] = i.condition

    return output

@app.get("/public/mod/{ids_array}")
async def public_mods(ids_array, catalog:bool = False):
    """
    Возвращает список публичных модов на сервере.
    Принимает массив ID модов. Возвращает масссив id's модов.
    Ограничение на разовый запрос - 50 элементов.
    """
    stc.update("/public/mod/")

    ids_array = tool.str_to_list(ids_array)

    if len(ids_array) < 1 or len(ids_array) > 50:
        return JSONResponse(status_code=413, content={"message": "the size of the array is not correct", "error_id": 1})

    print(ids_array)

    output = []

    # Создание сессии
    Session = sessionmaker(bind=sdc.engine)
    session = Session()

    # Выполнение запроса
    query = session.query(sdc.Mod)
    if catalog:
        query = query.filter(sdc.Mod.public == 0)
    else:
        query = query.filter(sdc.Mod.public <= 1)

    query = query.filter(sdc.Mod.id.in_(ids_array))
    for i in query:
        output.append(i.id)

    session.close()
    return output


@app.get("/statistics/delay")
async def statistics_delay():
    """
    Все данные возвращаются в миллисекундах *(int)*.

    Возвращает информацию о среднестатистической задержке при:
    1. `fast` - задержка обработки запроса о получении мода который есть на сервере.
    Важно понимать что сюда попадает только время затраченное на непосредственно обработку запроса сервером.
    2. `full` - полное время затраченное от начала обработки, до загрузки до состояния `1`
    *(т.е. не зарегистрирован, но доступен ядл скачивания)*.
    """
    stc.update("/statistics/delay/")

    # Создание сессии
    Session = sessionmaker(bind=stc.engine)
    session = Session()

    output = {}

    # Выполнение запроса FAST
    query = session.query(stc.ProcessingTime.delay).order_by(desc(stc.ProcessingTime.time))
    query = query.filter(stc.ProcessingTime.type.in_(["download_local_ok", "download_steam_ok"]))
    query = query.limit(20).all()
    if query != None and len(query) > 0:
        statist = []
        for i in query:
            statist.append(i.delay)
        output["fast"] = int(statistics.mean(statist))
    else:
        output["fast"] = 0

    # Выполнение запроса FULL
    query = session.query(stc.ProcessingTime.delay).order_by(desc(stc.ProcessingTime.time))
    query = query.filter(stc.ProcessingTime.type.in_(["steam_ok"]))
    query = query.limit(20).all()
    if query != None and len(query) > 0:
        statist = []
        for i in query:
            statist.append(i.delay)
        output["full"] = int(statistics.mean(statist))
    else:
        output["full"] = 0

    return output


@app.get("/statistics/hour")
async def statistics_hour(select_date: date = None, start_hour: int = 0, end_hour: int = 23):
    """
    Возвращает подробную статистику о запросах и работе сервера в конкретный день.

    Принимает необязательные параметры:
    1. `day` *(`YYYY-MM-DD`; `str`)* - день по которому нужна статистика. По умолчанию - сегодня.
    2. `start_hour` *(`int`)* - фильтрация по минимальному значению часа *(диапазон 0...23)*.
    3. `end_hour` *(`int`)* - фильтрация по максимальному значению часа *(диапазон 0...23)*.

    При фильтрации по часу отсекаются крайние значения, но не указанное.
    Т.е. - если указать в `start_hour` и в `end_hour` одно и тоже значение,
    то на выходе получите статистику только по этому часу.
    """
    stc.update("/statistics/hour/")
    if start_hour < 0 or start_hour > 23:
        return JSONResponse(status_code=412, content={"message": "start_hour exits 24 hour format", "error_id": 1})
    elif end_hour < 0 or end_hour > 23:
        return JSONResponse(status_code=412, content={"message": "end_hour exits 24 hour format", "error_id": 2})
    elif start_hour > end_hour:
        return JSONResponse(status_code=409, content={"message": "conflicting request", "error_id": 3})

    start_date = datetime.now().replace(hour=start_hour, minute=0, second=0, microsecond=0)
    end_date = datetime.now().replace(hour=end_hour, minute=0, second=0, microsecond=0)

    if select_date is date:
        start_date = start_date.replace(day=select_date.day, month=select_date.month, year=select_date.year)
        end_date = end_date.replace(day=select_date.day, month=select_date.month, year=select_date.year)

    Session = sessionmaker(bind=stc.engine)
    session = Session()

    query = session.query(stc.StatisticsHour.date_time, stc.StatisticsHour.count, stc.StatisticsHour.type).order_by(
        asc(stc.StatisticsHour.date_time))
    query = query.filter(stc.StatisticsHour.date_time >= start_date, stc.StatisticsHour.date_time <= end_date)

    output = []
    for i in query.all():
        output.append({"date_time": i.date_time, "type": i.type, "count": i.count})

    session.close()
    return output


@app.get("/statistics/day")
async def statistics_day(start_date: date = None, end_date: date = None):
    """
    Возвращает подробную статистику о запросах и работе сервера в конкретный день.

    Принимает необязательные параметры:
    1. `start_date` *(`YYYY-MM-DD`; `str`)* - день от начала которого нужна статистика *(включительно)*.
    По умолчанию = `end_date`-`7 days`.
    2. `end_date` *(`YYYY-MM-DD`; `str`)* - день до которого нужна статистика *(включительно)*.
    По умолчанию - текущая дата.

    При фильтрации по дня отсекаются крайние значения, но не указанные.
    Т.е. - если указать в `start_date` и в `end_date` одно и тоже значение,
    то на выходе получите статистику только по этому дню.
    """
    stc.update("/statistics/day/")
    if end_date is None:
        end_date = date.today()
    if start_date is None:
        start_date = end_date - timedelta(days=6)  # Т.к. у нас включительно, то именно 6 должно быть чтоб выходило 7
    if start_date > end_date:
        return JSONResponse(status_code=409, content={"message": "conflicting request", "error_id": 3})

    Session = sessionmaker(bind=stc.engine)
    session = Session()

    query = session.query(stc.StatisticsDay.date, stc.StatisticsDay.count, stc.StatisticsDay.type).order_by(
        asc(stc.StatisticsDay.date))
    query = query.filter(stc.StatisticsDay.date >= start_date, stc.StatisticsDay.date <= end_date)

    output = []
    for i in query.all():
        output.append({"date": i.date, "type": i.type, "count": i.count})

    session.close()
    return output


@app.get("/statistics/info/all")
async def statistics_info():
    """
    Возвращает общую информацию о состоянии базы данных. Не принимает аргументов.
    """
    stc.update("/statistics/info/all/")

    # Создание сессии
    Session = sessionmaker(bind=sdc.engine)
    session = Session()

    mod_count = session.query(sdc.Mod).count()
    game_count = session.query(sdc.Game).count()
    genres_count = session.query(sdc.Genres).count()
    mod_tag_count = session.query(sdc.ModTag).count()
    dependencies_count = session.query(func.count(func.distinct(sdc.mods_dependencies.c.mod_id))).scalar()
    total_mods_downloads = session.query(func.sum(sdc.Game.mods_downloads)).scalar()

    session.close()

    # Создание сессии
    Session = sessionmaker(bind=stc.engine)
    session = Session()

    days_count = session.query(func.count(func.distinct(stc.StatisticsDay.date))).scalar()

    session.close()

    return {"mods": mod_count, "games": game_count, "genres": genres_count, "mods_tags": mod_tag_count,
            "mods_dependencies": dependencies_count, "statistics_days": days_count,
            "mods_sent_count": total_mods_downloads}


@app.get("/statistics/info/type_map")
async def statistics_type_map(request: Request):
    """
    Возвращает карту переводов для типов в статистической ветке. Не принимает аргументов.
    Определяет на каком языке отправить ответ через поле `Accept-Language` в `headers` запроса.
    """
    stc.update("/statistics/info/type_map/")

    try:
        languages = [lang.split(";")[0].strip() for lang in request.headers.get("Accept-Language").split(",")]

        select_language = languages[0] if languages else "ru"
        for language in languages:
            if language in stc.allow_language_type_map:
                select_language = language
                break
        if select_language is None:
            select_language = "ru"
    except:
        select_language = "ru"

    # Ваш код для обработки языковых кодов
    # Например, вы можете вернуть список языковых кодов в формате JSON
    return {"language": select_language, "result": stc.cache_types_data(select_language)}


@app.post("/account/add/game")
async def account_add_game(request: Request, token: str, game_name: str = Form(...), game_short_desc: str = Form(...),
                           game_desc: str = Form(...), game_type: str = Form("game"), game_logo: str = Form("")):
    """
    Добавляет игры в базу.

    Типы приложенией не ограничены, но есть следующие "официально поддерживаемые" типы: `game`, `app`, `tool`.

    `game_logo` - это url на изображение.

    Local функция!

    Возвращает ID созданного элемента.
    """
    if not await access(request=request, user_token=token, real_token=config.token_add_game, func_name="add game"):
        return JSONResponse(status_code=403, content="Access denied. This case will be reported.")

    insert_statement = insert(sdc.Game).values(
        name=game_name,
        type=game_type,
        logo=game_logo,
        short_description=game_short_desc,
        description=game_desc,
        mods_downloads=0,
        mods_count=0,
        creation_date=datetime.now(),
        source='local'
    ).returning(sdc.Game.id)

    result = session.execute(insert_statement)
    id = result.fetchone()[0]  # Получаем значение `id` созданного элемента

    session.commit()
    session.close()

    return JSONResponse(status_code=202, content=id)  # Возвращаем значение `id`


@app.post("/account/add/genre")
async def account_add_genre(request: Request, token: str, genre_name: str = Form(...)):
    """
    Добавляет жанры в базу.

    Local функция!

    Возвращает ID созданного элемента.
    """
    if not await access(request=request, user_token=token, real_token=config.token_add_genre, func_name="add genre"):
        return JSONResponse(status_code=403, content="Access denied. This case will be reported.")

    insert_statement = insert(sdc.Genres).values(
        name=genre_name
    ).returning(sdc.Genres.id)

    result = session.execute(insert_statement)
    id = result.fetchone()[0]  # Получаем значение `id` созданного элемента

    session.commit()
    session.close()

    return JSONResponse(status_code=202, content=id)  # Возвращаем значение `id`


@app.post("/account/add/tag")
async def account_add_tag(request: Request, token: str, tag_name: str = Form(...)):
    """
    Добавляет теги в базу.

    Local функция!

    Возвращает ID созданного элемента.
    """
    if not await access(request=request, user_token=token, real_token=config.token_add_tag, func_name="add tag"):
        return JSONResponse(status_code=403, content="Access denied. This case will be reported.")

    insert_statement = insert(sdc.ModTag).values(
        name=tag_name
    ).returning(sdc.ModTag.id)

    result = session.execute(insert_statement)
    id = result.fetchone()[0]  # Получаем значение `id` созданного элемента

    session.commit()
    session.close()

    return JSONResponse(status_code=202, content=id)  # Возвращаем значение `id`


@app.post("/account/add/resource")
async def account_add_resource(request: Request, token: str, resource_type_name: str = Form(...),
                               resource_url: str = Form(...), resource_owner_id: int = Form(...)):
    """
    Добавляет ресурсы модов в базу.

    Типы ресурсов не ограничены, но есть следующие "официально поддерживаемые" типы: `logo`, `screenshot`.

    `resource_owner_id` - это id мода за которым закреплен ресурс.

    Local функция!

    Возвращает ID созданного элемента.
    """
    if not await access(request=request, user_token=token, real_token=config.token_add_resource, func_name="add resource"):
        return JSONResponse(status_code=403, content="Access denied. This case will be reported.")

    insert_statement = insert(sdc.ResourceMod).values(
        type=resource_type_name,
        url=resource_url,
        date_event=datetime.now(),
        owner_id=resource_owner_id
    ).returning(sdc.ResourceMod.id)

    result = session.execute(insert_statement)
    id = result.fetchone()[0]  # Получаем значение `id` созданного элемента

    session.commit()
    session.close()

    return JSONResponse(status_code=202, content=id)  # Возвращаем значение `id`


@app.post("/account/add/mod")
async def account_add_mod(request: Request, token: str, mod_name: str = Form(...),
                          mod_short_description: str = Form(...), mod_description: str = Form(...),
                          mod_source: str = Form(...), mod_game: int = Form(...), mod_public: int = Form(...),
                          mod_file: UploadFile = File(...)):
    """
    Добавляет моды в базу.
    Ограничение на архив - 800МБ. Ограничения на не сжатый размер мода нет.

    Публичность:

    `0` - Полностью публичен.

    `1` - Скрытый, но доступен по прямому запросу.

    `2` - Доступен только через микросервис account.

    `mod_source` - `local`, другие источники не предполагаются через эту функцию, но не ограничиваются.

    Local функция!

    Возвращает ID созданного элемента.
    """
    if not await access(request=request, user_token=token, real_token=config.token_add_mod, func_name="add mod"):
        return JSONResponse(status_code=403, content="Access denied. This case will be reported.")

    if mod_file.size >= 838860800:
        return JSONResponse(status_code=413, content="The file is too large.")
    elif not mod_file.filename.endswith(".zip"):
        return JSONResponse(status_code=400, content="Only ZIP archives are accepted.")

    if mod_public not in [0, 1, 2]:
        mod_public = 0

    insert_statement = insert(sdc.Mod).values(
        name=mod_name,
        short_description=mod_short_description,
        description=mod_description,

        condition=1,
        public=mod_public,

        date_creation=datetime.now(),
        date_update=datetime.now(),
        date_request=datetime.now(),

        source=mod_source,
        downloads=0,

        game=mod_game
    ).returning(sdc.Mod.id)

    result = session.execute(insert_statement)
    id = result.fetchone()[0]  # Получаем значение `id` созданного элемента

    session.commit()

    file_path = f"users_files_processing/{id}.zip"
    with open(file_path, "wb") as f:
        f.write(await mod_file.read())
    mod_size = await tool.calculate_uncompressed_size(file_path=file_path)

    archive_standart = await tool.zip_standart(archive_path=file_path)
    if len(archive_standart) <= 0:
        delete_statement = delete(sdc.Mod).where(sdc.Mod.id == id)
        session.execute(delete_statement)
        session.commit()

        return JSONResponse(status_code=500, content="An unknown error occurred while checking the archive standard.")

    Path(f"mods/{mod_game}").mkdir(parents=True, exist_ok=True)
    os.replace(src=archive_standart, dst=f"mods/{mod_game}/{id}.zip")

    session.query(sdc.Mod).filter_by(id=id).update({'condition': 0, 'size': mod_size})
    session.query(sdc.Game).filter_by(id=mod_game).update({'mods_count': tool.get_mods_count(session=session, game_id=mod_game)})
    session.commit()

    return JSONResponse(status_code=201, content=id)  # Возвращаем значение `id`


@app.post("/account/edit/game")
async def account_edit_game(request: Request, token: str, game_id: int, game_name: str = Form(None),
                            game_short_desc: str = Form(None), game_desc: str = Form(None), game_type: str = Form(None),
                            game_logo: str = Form(None), game_source: str = Form(None)):
    """
    Изменяет игры в базе. Принимает обязательно ID игры.

    Обязательно должен быть передать один из не обязательных параметров.

    Local функция!
    """
    if not await access(request=request, user_token=token, real_token=config.token_edit_game, func_name="edit game"):
        return JSONResponse(status_code=403, content="Access denied. This case will be reported.")

    game = session.query(sdc.Game).filter_by(id=game_id)
    if not game.first():
        return JSONResponse(status_code=404, content="The element does not exist.")

    # Подготавливаем данные
    data_edit = {}
    if game_name:
        data_edit["name"] = game_name
    if game_short_desc:
        data_edit["short_description"] = game_short_desc
    if game_desc:
        data_edit["description"] = game_desc
    if game_type:
        data_edit["type"] = game_type
    if game_logo:
        data_edit["logo"] = game_logo
    if game_source:
        data_edit["source"] = game_source

    if len(data_edit) <= 0:
        return JSONResponse(status_code=418, content="The request is empty")

    # Меняем данные в БД
    game.update(data_edit)
    session.commit()
    return JSONResponse(status_code=202, content="Complite")


@app.post("/account/edit/genre")
async def account_edit_genre(request: Request, token: str, genre_id: int, genre_name: str = Form(None)):
    """
    Изменяет жанр в базе. Принимает обязательно ID жанра.

    Обязательно должен быть передать один из не обязательных параметров.

    Local функция!
    """
    if not await access(request=request, user_token=token, real_token=config.token_edit_genre, func_name="edit genre"):
        return JSONResponse(status_code=403, content="Access denied. This case will be reported.")

    game = session.query(sdc.Genres).filter_by(id=genre_id)
    if game.first():
        return JSONResponse(status_code=404, content="The element does not exist.")

    # Подготавливаем данные
    data_edit = {}
    if genre_name:
        data_edit["name"] = genre_name

    if len(data_edit) <= 0:
        return JSONResponse(status_code=418, content="The request is empty")

    # Меняем данные в БД
    game = session.query(sdc.Genres).filter_by(id=genre_id)
    game.update(data_edit)
    session.commit()
    return JSONResponse(status_code=202, content="Complite")


@app.post("/account/edit/tag")
async def account_edit_tag(request: Request, token: str, tag_id: int, tag_name: str = Form(None)):
    """
    Изменяет теги в базе. Принимает обязательно ID тега.

    Обязательно должен быть передать один из не обязательных параметров.

    Local функция!
    """
    if not await access(request=request, user_token=token, real_token=config.token_edit_tag, func_name="edit tag"):
        return JSONResponse(status_code=403, content="Access denied. This case will be reported.")

    tag = session.query(sdc.ModTag).filter_by(id=tag_id)
    if not tag.first():
        return JSONResponse(status_code=404, content="The element does not exist.")

    # Подготавливаем данные
    data_edit = {}
    if tag_name:
        data_edit["name"] = tag_name

    if len(data_edit) <= 0:
        return JSONResponse(status_code=418, content="The request is empty")

    # Меняем данные в БД
    tag.update(data_edit)
    session.commit()
    return JSONResponse(status_code=202, content="Complite")


@app.post("/account/edit/resource")
async def account_edit_resource(request: Request, token: str, resource_id: int, resource_type: str = Form(None),
                                resource_url: str = Form(None), resource_owner_id: int = Form(None)):
    """
    Изменяет ресурс мода в базе. Принимает обязательно ID ресурса.

    Обязательно должен быть передать один из не обязательных параметров.

    Local функция!
    """
    if not await access(request=request, user_token=token, real_token=config.token_edit_resource, func_name="edit resource"):
        return JSONResponse(status_code=403, content="Access denied. This case will be reported.")

    resource = session.query(sdc.ResourceMod).filter_by(id=resource_id)
    if not resource.first():
        return JSONResponse(status_code=404, content="The element does not exist.")

    # Подготавливаем данные
    data_edit = {}
    if resource_type:
        data_edit["type"] = resource_type
    if resource_url:
        data_edit["url"] = resource_url
    if resource_owner_id:
        data_edit["owner_id"] = resource_owner_id

    if len(data_edit) <= 0:
        return JSONResponse(status_code=418, content="The request is empty")

    data_edit["date_event"] = datetime.now()

    # Меняем данные в БД
    resource.update(data_edit)
    session.commit()
    return JSONResponse(status_code=202, content="Complite")


@app.post("/account/edit/mod")
async def account_edit_mod(request: Request, token: str, mod_id: int, mod_name: str = Form(None),
                           mod_short_description: str = Form(None), mod_description: str = Form(None),
                           mod_source: str = Form(None), mod_game: int = Form(None), mod_public: int = Form(None),
                           mod_file: UploadFile = File(None)):
    """
    Изменяет моды в базе. Принимает обязательно ID мода.
    Ограничение на архив - 800МБ. Ограничения на не сжатый размер мода нет.

    Публичность:

    `0` - Полностью публичен.

    `1` - Скрытый, но доступен по прямому запросу.

    `2` - Доступен только через микросервис account.

    `mod_source` - `local`, другие источники не предполагаются через эту функцию, но не ограничиваются.

    Local функция!
    """
    if not await access(request=request, user_token=token, real_token=config.token_edit_mod, func_name="edit mod"):
        return JSONResponse(status_code=403, content="Access denied. This case will be reported.")

    mod = session.query(sdc.Mod).filter_by(id=mod_id)
    mod_data = mod.first()
    if not mod_data:
        return JSONResponse(status_code=404, content="The element does not exist.")

    if mod_file:
        if mod_file.size >= 838860800:
            return JSONResponse(status_code=413, content="The file is too large.")
        elif not mod_file.filename.endswith(".zip"):
            return JSONResponse(status_code=400, content="Only ZIP archives are accepted.")

    # Подготавливаем данные
    data_edit = {}
    if mod_name:
        data_edit["name"] = mod_name
    if mod_short_description is not None:
        data_edit["short_description"] = mod_short_description
    if mod_description is not None:
        data_edit["description"] = mod_description
    if mod_source:
        data_edit["source"] = mod_source
    if mod_public in [0, 1, 2]:
        data_edit["public"] = mod_public
    if mod_game:
        data_edit["game"] = mod_game
        mod_game_before = mod_data.game


    if mod_file:
        if mod_file.size >= 838860800:
            return JSONResponse(status_code=413, content="The file is too large.")
        elif not mod_file.filename.endswith(".zip"):
            return JSONResponse(status_code=400, content="Only ZIP archives are accepted.")

        file_path = f"users_files_processing/{mod_id}.zip"
        with open(file_path, "wb") as f:
            f.write(await mod_file.read())

        archive_standart = await tool.zip_standart(archive_path=file_path)
        if len(archive_standart) <= 0:
            return JSONResponse(status_code=500, content="An unknown error occurred while checking the archive standard.")
        else:
            data_edit["date_update"] = datetime.now()
            data_edit["size"] = await tool.calculate_uncompressed_size(file_path=archive_standart)

        Path(f"mods/{mod_data.game}").mkdir(parents=True, exist_ok=True)
        result_replace = os.replace(src=archive_standart, dst=f"mods/{mod_data.game}/{mod_id}.zip")
        print(f'Mod update (mod_file): Результат переноса в постоянное хранилище: {result_replace}', flush=True)

    if mod_game:
        Path(f"mods/{mod_game}").mkdir(parents=True, exist_ok=True)
        result_replace = os.replace(src=f"mods/{mod_data.game}/{mod_id}.zip", dst=f"mods/{mod_game}/{mod_id}.zip")
        print(f'Mod update (game): Результат переноса в постоянное хранилище: {result_replace}', flush=True)

    if len(data_edit) <= 0:
        return JSONResponse(status_code=418, content="The request is empty")

    data_edit["date_request"] = datetime.now()

    # Меняем данные в БД
    mod.update(data_edit)
    session.commit()

    if mod_game:
        session.query(sdc.Game).filter_by(id=mod_game_before).update({'mods_count': tool.get_mods_count(session=session, game_id=mod_game_before)})
        session.query(sdc.Game).filter_by(id=mod_game).update({'mods_count': tool.get_mods_count(session=session, game_id=mod_game)})
        session.commit()

    return JSONResponse(status_code=202, content="Complite")


@app.post("/account/delete/game")
async def account_delete_game(request: Request, token: str, game_id: int):
    """
    Удаляет игру в базе. Принимает обязательно ID игры.

    При удалении игры, автоматически удаляются и ассоциации жанров и тегов.
    Моды, которыми владеет игра сохраняются ради безопасности.

    Local функция!
    """
    if not await access(request=request, user_token=token, real_token=config.token_delete_game, func_name="delete game"):
        return JSONResponse(status_code=403, content="Access denied. This case will be reported.")

    delete_game = delete(sdc.Game).where(sdc.Game.id == game_id)

    delete_genres_association = sdc.game_genres.delete().where(sdc.game_genres.c.game_id == game_id)
    delete_tags_association = sdc.allowed_mods_tags.delete().where(sdc.allowed_mods_tags.c.game_id == game_id)

    # Выполнение операции DELETE
    session.execute(delete_game)
    session.execute(delete_genres_association)
    session.execute(delete_tags_association)
    session.commit()

    return JSONResponse(status_code=202, content="Complite")


@app.post("/account/delete/genre")
async def account_delete_genre(request: Request, token: str, genre_id: int):
    """
    Удаляет жанр в базе. Принимает обязательно ID жанра.

    При удалении жанра, автоматически удаляются и ассоциации его с играми.

    Local функция!
    """
    if not await access(request=request, user_token=token, real_token=config.token_delete_genre, func_name="delete genre"):
        return JSONResponse(status_code=403, content="Access denied. This case will be reported.")

    delete_game = delete(sdc.Genres).where(sdc.Genres.id == genre_id)

    delete_genres_association = sdc.game_genres.delete().where(sdc.game_genres.c.genre_id == genre_id)

    # Выполнение операции DELETE
    session.execute(delete_game)
    session.execute(delete_genres_association)
    session.commit()
    return JSONResponse(status_code=202, content="Complite")


@app.post("/account/delete/tag")
async def account_delete_tag(request: Request, token: str, tag_id: int):
    """
    Удаляет тег в базе. Принимает обязательно ID тега.

    При удалении тега, автоматически удаляются и ассоциации этого тега с жанрами и играми.

    Local функция!
    """
    if not await access(request=request, user_token=token, real_token=config.token_delete_tag, func_name="delete tag"):
        return JSONResponse(status_code=403, content="Access denied. This case will be reported.")

    delete_game = delete(sdc.ModTag).where(sdc.ModTag.id == tag_id)

    delete_mods_tags_association = sdc.mods_tags.delete().where(sdc.mods_tags.c.tag_id == tag_id)
    delete_game_tags_association = sdc.allowed_mods_tags.delete().where(sdc.allowed_mods_tags.c.tag_id == tag_id)

    # Выполнение операции DELETE
    session.execute(delete_game)
    session.execute(delete_mods_tags_association)
    session.execute(delete_game_tags_association)
    session.commit()
    return JSONResponse(status_code=202, content="Complite")


@app.post("/account/delete/resource")
async def account_delete_resource(request: Request, token: str, resource_id: int):
    """
    Удаляет ресурс мода в базе. Принимает обязательно ID ресурса.

    Local функция!
    """
    if not await access(request=request, user_token=token, real_token=config.token_delete_resource, func_name="delete resource"):
        return JSONResponse(status_code=403, content="Access denied. This case will be reported.")

    delete_resource = delete(sdc.ResourceMod).where(sdc.ResourceMod.id == resource_id)

    # Выполнение операции DELETE
    session.execute(delete_resource)
    session.commit()
    return JSONResponse(status_code=202, content="Complite")


@app.post("/account/delete/mod")
async def account_delete_mod(request: Request, token: str, mod_id: int):
    """
    Удаляет мод в базе. Принимает обязательно ID мода.

    При удалении мода, автоматически удаляются и ассоциации этого мода с жанрами, и тегами.
    Так же удаляются записи о ресурсах этого мода, и ZIP архив мода.

    Local функция!
    """
    if not await access(request=request, user_token=token, real_token=config.token_delete_mod, func_name="delete mod"):
        return JSONResponse(status_code=403, content="Access denied. This case will be reported.")

    try:
        query_game = session.query(sdc.Mod.game).filter(sdc.Mod.id == mod_id).first().game
        mod_path = f"mods/{query_game}/{mod_id}.zip"

        delete_game = delete(sdc.Mod).where(sdc.Mod.id == mod_id)
        delete_dependence_association = sdc.mods_dependencies.delete().where(sdc.mods_dependencies.c.mod_id == mod_id)
        delete_tags_association = sdc.mods_tags.delete().where(sdc.mods_tags.c.mod_id == mod_id)
        delete_resource = delete(sdc.ResourceMod).where(sdc.ResourceMod.owner_id == mod_id)


        # Выполнение операции DELETE
        session.execute(delete_game)
        session.execute(delete_dependence_association)
        session.execute(delete_tags_association)
        session.execute(delete_resource)
        session.commit()

        os.remove(mod_path)

        if session.query(sdc.Game).filter_by(id=query_game).first():
            session.query(sdc.Game).filter_by(id=query_game).update({'mods_count': tool.get_mods_count(session=session, game_id=query_game)})
        session.commit()

        return JSONResponse(status_code=202, content="Complite")
    except:
        return JSONResponse(status_code=500, content="Error")


@app.post("/account/association/game/genre")
async def account_association_game_genre(request: Request, token: str, game_id: int, mode: bool, genre_id: int):
    """
    Управляет ассоциацией жанров к играм в базе. Принимает обязательно ID игры и жанра.

    Режим работы (`mode`): `mode = True` - добавить ассоциацию; `mode = False` - удалить ассоциацию.

    Local функция!
    """
    if not await access(request=request, user_token=token, real_token=config.token_association_game_genre, func_name="association game genre"):
        return JSONResponse(status_code=403, content="Access denied. This case will be reported.")

    if mode:
        output = session.query(sdc.game_genres).filter_by(game_id=game_id, genre_id=genre_id).first()
        if output is None:
            insert_statement = insert(sdc.game_genres).values(game_id=game_id, genre_id=genre_id)
            session.execute(insert_statement)
            session.commit()
            return JSONResponse(status_code=202, content="Complite")
        else:
            return JSONResponse(status_code=409, content="The association is already present")
    else:
        delete_genre_association = sdc.game_genres.delete().where(sdc.game_genres.c.game_id == game_id,
                                                                  sdc.game_genres.c.genre_id == genre_id)

        # Выполнение операции DELETE
        session.execute(delete_genre_association)
        session.commit()
        return JSONResponse(status_code=202, content="Complite")


@app.post("/account/association/game/tag")
async def account_association_game_tag(request: Request, token: str, game_id: int, mode: bool, tag_id: int):
    """
    Управляет ассоциацией тегов к играм в базе. Принимает обязательно ID игры и тега.

    Режим работы (`mode`): `mode = True` - добавить ассоциацию; `mode = False` - удалить ассоциацию.

    Local функция!
    """
    if not await access(request=request, user_token=token, real_token=config.token_association_game_tag, func_name="association game tag"):
        return JSONResponse(status_code=403, content="Access denied. This case will be reported.")

    if mode:
        output = session.query(sdc.allowed_mods_tags).filter_by(game_id=game_id, tag_id=tag_id).first()
        if output is None:
            insert_statement = insert(sdc.allowed_mods_tags).values(game_id=game_id, tag_id=tag_id)
            session.execute(insert_statement)
            session.commit()
            return JSONResponse(status_code=202, content="Complite")
        else:
            return JSONResponse(status_code=409, content="The association is already present")
    else:
        delete_tags_association = sdc.allowed_mods_tags.delete().where(sdc.allowed_mods_tags.c.game_id == game_id,
                                                                       sdc.allowed_mods_tags.c.tag_id == tag_id)

        # Выполнение операции DELETE
        session.execute(delete_tags_association)
        session.commit()
        return JSONResponse(status_code=202, content="Complite")


@app.post("/account/association/mod/tag")
async def account_association_mod_tag(request: Request, token: str, mod_id: int, mode: bool, tag_id: int):
    """
    Управляет ассоциацией тега к модам в базе. Принимает обязательно ID мода и тега.

    Режим работы (`mode`): `mode = True` - добавить ассоциацию; `mode = False` - удалить ассоциацию.

    Local функция!
    """
    if not await access(request=request, user_token=token, real_token=config.token_association_mod_tag, func_name="association mod tag"):
        return JSONResponse(status_code=403, content="Access denied. This case will be reported.")

    if mode:
        output = session.query(sdc.mods_tags).filter_by(mod_id=mod_id, tag_id=tag_id).first()
        if output is None:
            insert_statement = insert(sdc.mods_tags).values(mod_id=mod_id, tag_id=tag_id)
            session.execute(insert_statement)
            session.commit()
            return JSONResponse(status_code=202, content="Complite")
        else:
            return JSONResponse(status_code=409, content="The association is already present")
    else:
        delete_tags_association = sdc.mods_tags.delete().where(sdc.mods_tags.c.mod_id == mod_id,
                                                               sdc.mods_tags.c.tag_id == tag_id)

        # Выполнение операции DELETE
        session.execute(delete_tags_association)
        session.commit()
        return JSONResponse(status_code=202, content="Complite")


@app.post("/account/association/mod/dependencie")
async def account_association_mod_dependencie(request: Request, token: str, mod_id: int, mode: bool, dependencie: int):
    """
    Управляет ассоциацией модов к своим зависимостям (других модам) в базе. Принимает обязательно ID мода и мода от которого он зависит.

    Режим работы (`mode`): `mode = True` - добавить ассоциацию; `mode = False` - удалить ассоциацию.

    Local функция!
    """
    if not await access(request=request, user_token=token, real_token=config.token_association_mod_dependencie, func_name="association mod dependencie"):
        return JSONResponse(status_code=403, content="Access denied. This case will be reported.")

    if mode:
        output = session.query(sdc.mods_dependencies).filter_by(mod_id=mod_id, dependence=dependencie).first()
        if output is None:
            insert_statement = insert(sdc.mods_dependencies).values(mod_id=mod_id, dependence=dependencie)
            session.execute(insert_statement)
            session.commit()
            return JSONResponse(status_code=202, content="Complite")
        else:
            return JSONResponse(status_code=409, content="The association is already present")
    else:
        delete_dependence_association = sdc.mods_dependencies.delete().where(sdc.mods_dependencies.c.mod_id == mod_id,
                                                                             sdc.mods_dependencies.c.dependence == dependencie)

        # Выполнение операции DELETE
        session.execute(delete_dependence_association)
        session.commit()
        return JSONResponse(status_code=202, content="Complite")


def init():
    steam = SteamCMD("steam_client")
    try:
        steam.install(force=True)
        print("Установка клиента Steam завершена")
    except SteamCMDException:
        print("Steam клиент уже установлен, попробуйте использовать параметр --force для принудительной установки")


if threads.get("start", None) == None:
    stc.update("start")
    threads["start"] = threading.Thread(target=init, name="start")
    threads["start"].start()

    # Создание сессии
    Session = sessionmaker(bind=sdc.engine)
    session = Session()

    query = session.query(sdc.Mod)
    query = query.filter(sdc.Mod.condition != 0).all()

    for mod in query:
        try:
            path = f'steamapps/workshop/content/{mod.associated_games[0].id}/{mod.id}'

            if os.path.isfile(f'mods/{mod.associated_games[0].id}/{mod.id}.zip'):
                print(f'Обнаружен не провалидированный архив! ({mod.id})')
                os.remove(f'mods/{mod.associated_games[0].id}/{mod.id}.zip')
        except:
            print(f"Ошибка удаления папки/архива битого мода с ID - {mod.id}", flush=True)

        # Если загрузка окончена ошибкой
        delete_statement = delete(sdc.Mod).where(sdc.Mod.id == int(mod.id))
        delete_tags = sdc.mods_tags.delete().where(sdc.mods_tags.c.mod_id == int(mod.id))
        delete_dep = sdc.mods_dependencies.delete().where(sdc.mods_dependencies.c.mod_id == int(mod.id))
        # Выполнение операции DELETE
        session.execute(delete_statement)
        session.execute(delete_tags)
        session.execute(delete_dep)
        session.commit()
    session.close()

    # Удаляем всю папку т.к. там только поврежденные моды
    if os.path.isdir("steamapps/workshop"):
        shutil.rmtree("steamapps/workshop")
        print("Папка модов Steam удалена", flush=True)

    if os.path.isdir("users_files_processing"):
        shutil.rmtree("users_files_processing")
        Path("users_files_processing").mkdir(parents=True, exist_ok=True)
        print("Папка пользовательских не обработанных модов удалена", flush=True)

    threads["todo"] = threading.Thread(target=todo_exe, name="todo")
    threads["todo"].start()
