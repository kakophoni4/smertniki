from aiogram.fsm.state import State, StatesGroup


class Form(StatesGroup):
    wait_ogrn_one = State()
    wait_ogrn_list = State()
    wait_ogrn_file = State()
    wait_remove_ogrn = State()
    wait_check_ogrn = State()
    wait_inn_one = State()
    wait_inn_list = State()
    wait_inn_file = State()
    wait_add_user = State()
    wait_remove_user = State()
