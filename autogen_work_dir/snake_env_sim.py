import random
from snake_ai import get_state

def step(state, action, grid_size, snake_head, snake_body, food_position):
    # Unpack state (not used directly, but kept for consistency)
    food_x_relative, food_y_relative, danger_up, danger_down, danger_left, danger_right = state

    # Initialize next state and reward
    reward = -0.1  # default reward
    game_over = False
    new_head = None

    # Calculate new head position based on action
    if action == 'up':
        new_head = (snake_head[0], snake_head[1] - 1)
    elif action == 'down':
        new_head = (snake_head[0], snake_head[1] + 1)
    elif action == 'left':
        new_head = (snake_head[0] - 1, snake_head[1])
    elif action == 'right':
        new_head = (snake_head[0] + 1, snake_head[1])

    # Check for collisions (wall and self)
    if (new_head[0] < 0 or new_head[0] >= grid_size or
        new_head[1] < 0 or new_head[1] >= grid_size or
        new_head in snake_body[1:]):
        reward = -10
        game_over = True
        return state, reward, game_over, snake_head, snake_body, food_position #Return old values

    # Update snake body
    new_snake_body = [new_head] + snake_body[:-1]
    snake_head = new_head
    snake_body = new_snake_body

    # Check for food consumption
    if new_head == food_position:
        reward = 10
        # Generate new food position
        food_position = (random.randint(0, grid_size - 1), random.randint(0, grid_size - 1))
        while food_position in snake_body:
            food_position = (random.randint(0, grid_size - 1), random.randint(0, grid_size - 1))

        # Add new segment to snake (don't remove the tail in this step)
        snake_body = [new_head] + snake_body

    # Update the state
    next_state = get_state(snake_head, food_position, snake_body, grid_size)

    return next_state, reward, game_over, snake_head, snake_body, food_position


def init_game(grid_size):
  snake_head = (random.randint(0, grid_size-1), random.randint(0, grid_size-1))
  food_position = (random.randint(0, grid_size-1), random.randint(0, grid_size-1))
  snake_body = [snake_head]

  #Ensure food is not on snake
  while food_position in snake_body:
    food_position = (random.randint(0, grid_size-1), random.randint(0, grid_size-1))

  return snake_head, food_position, snake_body
