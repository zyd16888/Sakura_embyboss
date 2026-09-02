from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup


class InlineButton(InlineKeyboardButton):
    def __init__(self, text=None, callback_data=None, **kwargs):
        super().__init__(text=text, callback_data=callback_data, **kwargs)


class InlineKeyboard(InlineKeyboardMarkup):
    """项目原 pykeyboard 用法的最小兼容实现。"""

    def __init__(self, row_width=3):
        self.inline_keyboard = []
        self.row_width = row_width
        super().__init__(inline_keyboard=self.inline_keyboard)

    def add(self, *buttons):
        self.inline_keyboard = [
            list(buttons[i:i + self.row_width])
            for i in range(0, len(buttons), self.row_width)
        ]

    def row(self, *buttons):
        self.inline_keyboard.append(list(buttons))

    def paginate(self, count_pages: int, current_page: int, callback_pattern: str):
        if count_pages <= 0:
            return

        def button(label, page):
            return InlineButton(str(label), callback_pattern.format(number=page))

        if count_pages <= 5:
            buttons = [
                button(f'· {page} ·' if page == current_page else page, page)
                for page in range(1, count_pages + 1)
            ]
        elif current_page <= 3:
            buttons = []
            for page in range(1, 6):
                if page == current_page:
                    buttons.append(button(f'· {page} ·', page))
                elif page == 4:
                    buttons.append(button(f'{page} ›', page))
                elif page == 5:
                    buttons.append(button(f'{count_pages} »', count_pages))
                else:
                    buttons.append(button(page, page))
        elif current_page > count_pages - 3:
            buttons = [button('« 1', 1),
                       button(f'‹ {count_pages - 3}', count_pages - 3)]
            buttons.extend(
                button(f'· {page} ·' if page == current_page else page, page)
                for page in range(count_pages - 2, count_pages + 1)
            )
        else:
            buttons = [
                button('« 1', 1),
                button(f'‹ {current_page - 1}', current_page - 1),
                button(f'· {current_page} ·', current_page),
                button(f'{current_page + 1} ›', current_page + 1),
                button(f'{count_pages} »', count_pages),
            ]
        self.inline_keyboard.append(buttons)
