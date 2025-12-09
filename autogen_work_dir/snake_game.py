
import random

class Grid:
    EMPTY = 0
    SNAKE = 1
    FOOD = 2

    def __init__(self, width, height):
        self.width = width
        self.height = height
        self.grid = [[Grid.EMPTY for _ in range(width)] for _ in range(height)]

    def place_food(self, row, col):
        if 0 <= row < self.height and 0 <= col < self.width:
            self.grid[row][col] = Grid.FOOD
        else:
            raise ValueError("Invalid food position")

    def place_food_randomly(self, snake_body):
        empty_cells = []
        for row in range(self.height):
            for col in range(self.width):
                if self.grid[row][col] == Grid.EMPTY and (row, col) not in snake_body:
                    empty_cells.append((row, col))

        if not empty_cells:
            return False  # Grid is full

        row, col = random.choice(empty_cells)
        self.place_food(row, col)
        return True

    def is_valid_position(self, row, col):
        return 0 <= row < self.height and 0 <= col < self.width and self.grid[row][col] != Grid.SNAKE

    def reset_grid(self):
        self.grid = [[Grid.EMPTY for _ in range(self.width)] for _ in range(self.height)]

    def update_grid(self, snake_body):
        self.reset_grid()
        for row, col in snake_body:
            self.grid[row][col] = Grid.SNAKE

    def get_grid(self):
        return self.grid

class Snake:
    def __init__(self, initial_position, initial_direction):
        self.body = [initial_position]  # Snake starts with one segment
        self.direction = initial_direction

    def move(self):
        head_row, head_col = self.get_head_position()
        d_row, d_col = self.direction
        new_head_row = head_row + d_row
        new_head_col = head_col + d_col
        self.body.insert(0, (new_head_row, new_head_col))
        self.body.pop()  # Remove tail segment

    def grow(self):
        head_row, head_col = self.get_head_position()
        d_row, d_col = self.direction
        new_head_row = head_row + d_row
        new_head_col = head_col + d_col
        self.body.insert(0, (new_head_row, new_head_col))

    def get_head_position(self):
        return self.body[0]

    def get_body(self):
        return self.body

    def set_direction(self, new_direction):
        self.direction = new_direction
