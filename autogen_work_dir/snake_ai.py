import time
import os
import pickle

class ModelPersistenceHandler:
    """
    A class for handling the saving and loading of SnakeAI models.
    """

    @staticmethod
    def save_model(model, filepath):
        """
        Saves the given model to the specified filepath using pickle.

        Args:
            model: The SnakeAI model to save.
            filepath: The path to the file where the model should be saved.
        """
        try:
            with open(filepath, 'wb') as f:
                pickle.dump(model, f)
        except Exception as e:
            print(f"Error saving model to {filepath}: {e}")
            raise

    @staticmethod
    def load_model(filepath):
        """
        Loads a SnakeAI model from the specified filepath using pickle.

        Args:
            filepath: The path to the file from which the model should be loaded.

        Returns:
            The loaded SnakeAI model.
        """
        try:
            with open(filepath, 'rb') as f:
                model = pickle.load(f)
            return model
        except FileNotFoundError:
            print(f"Error: File not found at {filepath}")
            return None  # Or raise an exception
        except Exception as e:
            print(f"Error loading model from {filepath}: {e}")
            return None # Or raise an exception


class SnakeAI:
    def __init__(self, q_table=None, epsilon=1.0, epsilon_decay=0.999, learning_rate=0.1, discount_factor=0.9, name=None):
        self.q_table = q_table if q_table is not None else {}
        self.epsilon = epsilon
        self.epsilon_decay = epsilon_decay
        self.learning_rate = learning_rate
        self.discount_factor = discount_factor
        self.name = name if name is not None else f"SnakeAI_unnamed_{time.strftime('%Y%m%d_%H%M%S')}"

    def set_name(self, name):
        """Sets a custom name for the model."""
        self.name = name

    def get_state(self, snake_head, food_position, snake_body, grid_size):
        # Discretize relative food position
        food_x_diff = food_position[0] - snake_head[0]
        food_y_diff = food_position[1] - snake_head[1]

        if food_x_diff > 2: food_x_relative = 'right'
        elif food_x_diff < -2: food_x_relative = 'left'
        else: food_x_relative = 'close_x'

        if food_y_diff > 2: food_y_relative = 'down'
        elif food_y_diff < -2: food_y_relative = 'up'
        else: food_y_relative = 'close_y'

        # Determine danger in each direction
        # Danger = wall or self
        # Helper to check collision
        def is_collision(x, y):
            # Wall
            if x < 0 or x >= grid_size or y < 0 or y >= grid_size:
                return True
            # Body (ignore tail as it moves?) - usually simple check includes tail
            if (x, y) in snake_body[1:]:
                 return True
            return False

        danger_up = is_collision(snake_head[0], snake_head[1] - 1)
        danger_down = is_collision(snake_head[0], snake_head[1] + 1)
        danger_left = is_collision(snake_head[0] - 1, snake_head[1])
        danger_right = is_collision(snake_head[0] + 1, snake_head[1])

        # Return the state as a tuple
        return (food_x_relative, food_y_relative, danger_up, danger_down, danger_left, danger_right)


    def save(self, filepath):
        """Saves the SnakeAI model to the specified filepath."""
        try:
            ModelPersistenceHandler.save_model(self, filepath)
            print(f"Model saved to {filepath}")
        except Exception as e:
            print(f"Error saving model: {e}")
            raise

    @staticmethod
    def load(filepath):
        """Loads a SnakeAI model from the specified filepath.

        Returns:
            The loaded SnakeAI model, or None if loading fails.
        """
        try:
            loaded_model = ModelPersistenceHandler.load_model(filepath)
            if loaded_model:
                # Initialize a new SnakeAI instance with the loaded model's attributes
                snake_ai = SnakeAI(
                    q_table=loaded_model.q_table,
                    epsilon=loaded_model.epsilon,
                    epsilon_decay=loaded_model.epsilon_decay,
                    learning_rate=loaded_model.learning_rate,
                    discount_factor=loaded_model.discount_factor,
                    name=loaded_model.name
                )
                print(f"Model loaded from {filepath}")
                return snake_ai
            else:
                return None
        except FileNotFoundError:
            print(f"Error: Model file not found at {filepath}")
            return None
        except Exception as e:
            print(f"Error loading model: {e}")
            return None