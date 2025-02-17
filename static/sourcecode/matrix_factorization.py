from typing import Optional, Tuple

import constants as c

import pandas as pd
import torch


class BiasedMatrixFactorization(torch.nn.Module):
  """Matrix factorization algorithm class."""

  def __init__(
    self, n_users: int, n_items: int, n_factors: int = 1, use_global_intercept: bool = True
  ) -> None:
    """Initialize matrix factorization model using xavier_uniform for factors
    and zeros for intercepts.

    Args:
        n_users (int): number of raters
        n_items (int): number of notes
        n_factors (int, optional): number of dimensions. Defaults to 1. Only 1 is supported.
        use_global_intercept (bool, optional): Defaults to True.
    """
    super().__init__()
    self.user_factors = torch.nn.Embedding(n_users, n_factors, sparse=False)
    self.item_factors = torch.nn.Embedding(n_items, n_factors, sparse=False)
    self.user_intercepts = torch.nn.Embedding(n_users, 1, sparse=False)
    self.item_intercepts = torch.nn.Embedding(n_items, 1, sparse=False)
    self.use_global_intercept = use_global_intercept
    self.global_intercept = torch.nn.parameter.Parameter(torch.zeros(1, 1))
    torch.nn.init.xavier_uniform_(self.user_factors.weight)
    torch.nn.init.xavier_uniform_(self.item_factors.weight)
    self.user_intercepts.weight.data.fill_(0.0)
    self.item_intercepts.weight.data.fill_(0.0)

  def forward(self, user, item):
    """Forward pass: get predicted rating for user of note (item)"""
    pred = self.user_intercepts(user) + self.item_intercepts(item)
    pred += (self.user_factors(user) * self.item_factors(item)).sum(1, keepdim=True)
    if self.use_global_intercept == True:
      pred += self.global_intercept
    return pred.squeeze()


def run_mf(
  ratings: pd.DataFrame,
  l2_lambda: float,
  l2_intercept_multiplier: float,
  numFactors: int,
  epochs: int,
  useGlobalIntercept: bool,
  runName: str = "prod",
  logging: bool = True,
  flipFactorsForIdentification: bool = True,
) -> Tuple[pd.DataFrame, pd.DataFrame, Optional[float]]:
  """Train matrix factorization model.

  See https://twitter.github.io/birdwatch/ranking-notes/#matrix-factorization

  Args:
      ratings (pd.DataFrame): pre-filtered ratings to train on
      l2_lambda (float): regularization for factors
      l2_intercept_multiplier (float): how much extra to regularize intercepts
      numFactors (int): number of dimensions (only 1 is implemented)
      epochs (int): number of rounds of training
      useGlobalIntercept (bool): whether to fit global intercept parameter
      runName (str, optional): name. Defaults to "prod".
      logging (bool, optional): debug output. Defaults to True.
      flipFactorsForIdentification (bool, optional): Default to True.

  Returns:
      Tuple[pd.DataFrame, pd.DataFrame, float]:
        noteParams: contains one row per note, including noteId and learned note parameters
        raterParams: contains one row per rating, including raterId and learned rater parameters
        globalIntercept: learned global intercept parameter
  """
  assert numFactors == 1
  noteData = ratings
  noteData.dropna(0, inplace=True)

  noteIdMap = (
    pd.DataFrame(noteData[c.noteIdKey].unique())
    .reset_index()
    .set_index(0)
    .reset_index()
    .rename(columns={0: c.noteIdKey, "index": c.noteIndexKey})
  )
  raterIdMap = (
    pd.DataFrame(noteData[c.raterParticipantIdKey].unique())
    .reset_index()
    .set_index(0)
    .reset_index()
    .rename(columns={0: c.raterParticipantIdKey, "index": c.raterIndexKey})
  )

  noteRatingIds = noteData.merge(noteIdMap, on=c.noteIdKey)
  noteRatingIds = noteRatingIds.merge(raterIdMap, on=c.raterParticipantIdKey)

  n_users = noteRatingIds[c.raterIndexKey].nunique()
  n_items = noteRatingIds[c.noteIndexKey].nunique()
  if logging:
    print("------------------")
    print(f"Users: {n_users}, Notes: {n_items}")

  criterion = torch.nn.MSELoss()

  l2_lambda_intercept = l2_lambda * l2_intercept_multiplier

  rating = torch.FloatTensor(noteRatingIds[c.helpfulNumKey].values)
  row = torch.LongTensor(noteRatingIds[c.raterIndexKey].values)
  col = torch.LongTensor(noteRatingIds[c.noteIndexKey].values)

  mf_model = BiasedMatrixFactorization(
    n_users, n_items, use_global_intercept=useGlobalIntercept, n_factors=numFactors
  )
  optimizer = torch.optim.Adam(mf_model.parameters(), lr=1)  # learning rate

  def print_loss():
    y_pred = mf_model(row, col)
    train_loss = criterion(y_pred, rating)

    if logging:
      print("epoch", epoch, loss.item())
      print("TRAIN FIT LOSS: ", train_loss.item())

  for epoch in range(epochs):
    # Set gradients to zero
    optimizer.zero_grad()

    # Predict and calculate loss
    y_pred = mf_model(row, col)
    loss = criterion(y_pred, rating)
    l2_reg_loss = torch.tensor(0.0)

    for name, param in mf_model.named_parameters():
      if "intercept" in name:
        l2_reg_loss += l2_lambda_intercept * (param**2).mean()
      else:
        l2_reg_loss += l2_lambda * (param**2).mean()

    loss += l2_reg_loss

    # Backpropagate
    loss.backward()

    # Update the parameters
    optimizer.step()

    if epoch % 50 == 0:
      print_loss()

  print_loss()

  assert mf_model.item_factors.weight.data.numpy().shape[0] == noteIdMap.shape[0]

  noteIdMap[c.noteFactor1Key] = mf_model.item_factors.weight.data.numpy()[:, 0]
  raterIdMap[c.raterFactor1Key] = mf_model.user_factors.weight.data.numpy()[:, 0]
  noteIdMap[c.noteInterceptKey] = mf_model.item_intercepts.weight.data.numpy()
  raterIdMap[c.raterInterceptKey] = mf_model.user_intercepts.weight.data.numpy()

  globalIntercept = None
  if useGlobalIntercept:
    globalIntercept = mf_model.global_intercept

  if flipFactorsForIdentification:
    noteIdMap, raterIdMap = flip_factors_for_identification(noteIdMap, raterIdMap)

  return noteIdMap, raterIdMap, globalIntercept


def flip_factors_for_identification(
  noteParams: pd.DataFrame, raterParams: pd.DataFrame
) -> Tuple[pd.DataFrame, pd.DataFrame]:
  """Flip factors if needed, so that the larger group of raters gets a negative factor1

  Args:
      noteParams (pd.DataFrame): note params
      raterParams (pd.DataFrame): rater params

  Returns:
      Tuple[pd.DataFrame, pd.DataFrame]: noteParams, raterParams
  """
  raterFactors = raterParams.loc[~pd.isna(raterParams["raterFactor1"]), "raterFactor1"]
  propNegativeRaterFactors = (raterFactors < 0).sum() / (raterFactors != 0).sum()

  if propNegativeRaterFactors < 0.5:
    # Flip all factors, on notes and raters
    noteParams["noteFactor1"] = noteParams["noteFactor1"] * -1
    raterParams["raterFactor1"] = raterParams["raterFactor1"] * -1

  raterFactors = raterParams.loc[~pd.isna(raterParams["raterFactor1"]), "raterFactor1"]
  propNegativeRaterFactors = (raterFactors < 0).sum() / (raterFactors != 0).sum()
  assert propNegativeRaterFactors >= 0.5

  return noteParams, raterParams
