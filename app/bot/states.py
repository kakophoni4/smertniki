from aiogram.fsm.state import State, StatesGroup


class Form(StatesGroup):
    wait_inn_one = State()
    wait_inn_list = State()
    wait_inn_file = State()
    wait_remove_inn = State()
    wait_check_inn = State()
    wait_add_user = State()
    wait_remove_user = State()
    wait_make_admin = State()
    wait_revoke_admin = State()
