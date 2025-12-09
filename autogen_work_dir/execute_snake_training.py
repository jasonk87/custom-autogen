import argparse
import os
import time
import logging
import random
from snake_ai import SnakeAI  # Assuming snake_ai.py is in the same directory
from snake_env_sim import step, init_game, get_state # Assuming snake_env_sim.py is in the same directory

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Hyperparameters
alpha = 0.1
gamma = 0.9
epsilon = 0.2
grid_size = 10
num_episodes = 2000
save_interval = 500  # Save the model every 500 episodes

def train_snake_ai(snake_ai, num_episodes, save_interval):
    Q = snake_ai.q_table # Use the Q-table from the loaded model or the new model

    def choose_action(state):
        if state not in Q:
            Q[state] = {'up': 0, 'down': 0, 'left': 0, 'right': 0}

        if random.random() < snake_ai.epsilon:
            # Explore: choose a random action
            return random.choice(['up', 'down', 'left', 'right'])
        else:
            # Exploit: choose the action with the highest Q-value for the current state
            return max(Q[state], key=Q[state].get)

    def update_q_table(state, action, reward, next_state):
        if next_state not in Q:
            Q[next_state] = {'up': 0, 'down': 0, 'left': 0, 'right': 0}

        best_next_action = max(Q[next_state], key=Q[next_state].get)
        Q[state][action] = Q[state][action] + alpha * (reward + gamma * Q[next_state][best_next_action] - Q[state][action])

    # Training loop
    logging.info(f"Starting training for {num_episodes} episodes...")
    start_time = time.time()
    for episode in range(num_episodes):
        snake_head, food_position, snake_body = init_game(grid_size)
        state = get_state(snake_head, food_position, snake_body, grid_size)
        game_over = False

        steps = 0
        while not game_over and steps < 1000: # Limit steps to prevent infinite loops
            action = choose_action(state)
            # Pass all required state variables to step
            next_state, reward, game_over, snake_head, snake_body, food_position = step(state, action, grid_size, snake_head, snake_body, food_position)

            update_q_table(state, action, reward, next_state)
            state = next_state
            steps += 1

        if (episode + 1) % 50 == 0:
            logging.info(f"Episode {episode + 1}/{num_episodes} completed")

        # Save the model periodically
        if (episode + 1) % save_interval == 0:
            model_filename = f"{snake_ai.name}_{time.strftime('%Y%m%d_%H%M%S')}_epoch_{episode + 1}.pkl"
            model_filepath = os.path.join("models", model_filename)
            try:
                snake_ai.q_table = Q  # Update the snake_ai's Q-table before saving
                snake_ai.save(model_filepath)
                logging.info(f"Saved model to {model_filepath}")
            except Exception as e:
                logging.error(f"Error saving model: {e}")


    end_time = time.time()
    training_time = end_time - start_time
    logging.info(f"Training finished in {training_time:.2f} seconds!")
    return Q

if __name__ == '__main__':
    # Create argument parser
    parser = argparse.ArgumentParser(description='Train a Snake AI model.')
    parser.add_argument('--load_model', type=str, help='Path to a pre-trained model to load.')
    parser.add_argument('--model_name', type=str, default='unnamed_model', help='Name of the model.')
    args = parser.parse_args()

    # Create the models directory if it doesn't exist
    os.makedirs("models", exist_ok=True)

    # Load model if specified
    if args.load_model:
        try:
            snake_ai = SnakeAI.load(args.load_model)
            if snake_ai:
                logging.info(f"Loaded model from {args.load_model}")
                snake_ai.set_name(args.model_name) # Override model name with command line argument
            else:
                logging.error(f"Failed to load model from {args.load_model}, starting with a new model.")
                snake_ai = SnakeAI(name=args.model_name)
        except Exception as e:
            logging.error(f"Error loading model from {args.load_model}: {e}, starting with a new model.")
            snake_ai = SnakeAI(name=args.model_name)
    else:
        # Initialize a new SnakeAI model
        snake_ai = SnakeAI(name=args.model_name)

    # Train the model
    Q = train_snake_ai(snake_ai, num_episodes, save_interval)

    # Save the final model
    model_filename = f"{snake_ai.name}_{time.strftime('%Y%m%d_%H%M%S')}_final.pkl"
    model_filepath = os.path.join("models", model_filename)
    try:
        snake_ai.q_table = Q #Update the snake_ai's Q-table before saving
        snake_ai.save(model_filepath)
        logging.info(f"Saved final model to {model_filepath}")
    except Exception as e:
        logging.error(f"Error saving final model: {e}")
