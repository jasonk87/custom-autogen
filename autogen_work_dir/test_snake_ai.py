
import unittest
import os
from snake_ai import SnakeAI, ModelPersistenceHandler  # Import ModelPersistenceHandler

class TestSnakeAIModelPersistence(unittest.TestCase):

    def setUp(self):
        self.model = SnakeAI(q_table={(1,2,3,4,5,6): {0: 0.1, 1: 0.2, 2:0.3}},
                             epsilon=0.5,
                             epsilon_decay=0.9,
                             learning_rate=0.2,
                             discount_factor=0.8,
                             name="TestModel")
        self.filepath = "test_model.pkl"

    def tearDown(self):
        if os.path.exists(self.filepath):
            os.remove(self.filepath)

    def test_save_load_model(self):
        # Save the model
        self.model.save(self.filepath)

        # Load the model
        loaded_model = SnakeAI.load(self.filepath)

        # Assert that the loaded model is not None
        self.assertIsNotNone(loaded_model)

        # Assert that the loaded model's attributes match the original model
        self.assertEqual(loaded_model.name, self.model.name)
        self.assertEqual(loaded_model.q_table, self.model.q_table)
        self.assertEqual(loaded_model.epsilon, self.model.epsilon)
        self.assertEqual(loaded_model.epsilon_decay, self.model.epsilon_decay)
        self.assertEqual(loaded_model.learning_rate, self.model.learning_rate)
        self.assertEqual(loaded_model.discount_factor, self.model.discount_factor)

    def test_load_model_file_not_found(self):
        # Attempt to load a model from a non-existent file
        loaded_model = SnakeAI.load("non_existent_model.pkl")

        # Assert that the loaded model is None
        self.assertIsNone(loaded_model)

    def test_save_load_model_corrupted_file(self):
        # Create a corrupted file
        with open(self.filepath, "w") as f:
            f.write("This is a corrupted file")

        # Attempt to load the model from the corrupted file
        loaded_model = SnakeAI.load(self.filepath)

        # Assert that the loaded model is None
        self.assertIsNone(loaded_model)

if __name__ == '__main__':
    unittest.main()
